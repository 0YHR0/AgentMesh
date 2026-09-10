from __future__ import annotations

import ast
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from uuid import UUID, uuid4

import pytest

from agentmesh.application.authority_cohorts import AuthorityCohort
from agentmesh.application.coordinated_runtime import CoordinatedRuntimeAggregate
from agentmesh.application.coordinated_runtime_dispatch import (
    CoordinatedRuntimeDispatchKind,
    CoordinatedRuntimeDispatchService,
    CoordinatedRuntimePrepareKind,
)
from agentmesh.application.runtime_snapshots import assignment_snapshot_for
from agentmesh.domain.coordination import (
    CoordinationRuntimeBoundary,
    Subtask,
)
from agentmesh.domain.errors import InvalidTaskTransition, RuntimeExecutionConflict
from agentmesh.domain.runtime_execution import (
    RuntimeExecution,
    RuntimeTrustProfile,
    RuntimeVersion,
    RuntimeVersionStatus,
)
from agentmesh.domain.tasks import RunRole, Task, TaskAttempt, TaskExecutionMode, TaskRun
from agentmesh.runtime_sdk import RuntimeAssignment
from agentmesh.runtime_sdk.builtin import (
    LANGGRAPH_V2_DESCRIPTOR,
    builtin_langgraph_runtime_id,
    builtin_langgraph_version_id,
)
from agentmesh.runtime_sdk.canonical import canonical_digest, thaw_json

UTC = timezone.utc


def _version(now: datetime) -> RuntimeVersion:
    return RuntimeVersion(
        id=builtin_langgraph_version_id("v2"),
        runtime_id=builtin_langgraph_runtime_id(),
        api_version=1,
        adapter_kind="python-in-process",
        artifact_digest=canonical_digest(
            {"package": "agentmesh", "runtime": "agentmesh.langgraph", "release": "v2"}
        ),
        configuration_digest=canonical_digest(
            {
                "runtime_key": LANGGRAPH_V2_DESCRIPTOR["runtime_key"],
                "capabilities": LANGGRAPH_V2_DESCRIPTOR["capabilities"],
                "limits": LANGGRAPH_V2_DESCRIPTOR["limits"],
            }
        ),
        descriptor=LANGGRAPH_V2_DESCRIPTOR,
        trust_profile=RuntimeTrustProfile.BUILT_IN,
        compatibility={},
        status=RuntimeVersionStatus.PUBLISHED,
        created_at=now - timedelta(minutes=1),
        published_at=now - timedelta(minutes=1),
    )


def _chain() -> tuple[datetime, Task, Subtask, TaskRun, TaskAttempt, RuntimeVersion]:
    now = datetime.now(UTC) + timedelta(seconds=10)
    task = Task.create(
        tenant_id="tenant-a",
        objective="coordinated",
        execution_mode=TaskExecutionMode.COORDINATED,
        plan_version=1,
        plan_digest="sha256:plan",
        max_concurrency=2,
    )
    task.start_coordination(at=now - timedelta(seconds=5))
    subtask = Subtask.create(
        subtask_id=uuid4(),
        task_id=task.id,
        key="worker",
        objective="worker",
        input={},
        required_capabilities=("general.task",),
        preferred_agent_id=None,
        initially_ready=True,
    )
    version = _version(now)
    run = TaskRun.request(
        task.id,
        "agent",
        agent_version_id=uuid4(),
        agent_version_digest="a" * 64,
        role=RunRole.EXECUTOR,
        subtask_id=subtask.id,
        runtime_version_id=version.id,
        runtime_authority="managed",
        at=now - timedelta(seconds=4),
    )
    subtask.queue(run.id, at=now - timedelta(seconds=3))
    subtask.start(run.id, at=now - timedelta(seconds=2))
    run.start(at=now - timedelta(seconds=2))
    attempt = TaskAttempt.lease(
        run_id=run.id,
        worker_id="worker",
        fencing_token=7,
        lease_expires_at=now + timedelta(minutes=10),
    )
    attempt = replace(
        attempt,
        started_at=now - timedelta(seconds=2),
        heartbeat_at=now - timedelta(seconds=1),
    )
    return now, task, subtask, run, attempt, version


def _assignment(task: Task, run: TaskRun, version: RuntimeVersion) -> RuntimeAssignment:
    from agentmesh.runtime_sdk.descriptor import RuntimeDescriptor

    return RuntimeAssignment(
        assignment_id=str(uuid4()),
        tenant_id=task.tenant_id,
        task_id=str(task.id),
        run_id=str(run.id),
        agent_definition_id=str(uuid4()),
        agent_version_id=str(run.agent_version_id),
        agent_version_digest=run.agent_version_digest or "",
        runtime_version_id=str(version.id),
        runtime_descriptor_digest=RuntimeDescriptor.from_dict(
            thaw_json(version.descriptor)
        ).digest(),
        execution_mode="inline",
        run_role=run.role.value,
        revision=run.revision_number,
        structured_input={"prompt": "hello"},
        correlation_ids={"runtime_execution_id": str(run.runtime_execution_intent_id)},
    )


class _Uow:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.runtimes = SimpleNamespace(
            add_execution=lambda value: self.events.append("execution.add"),
            add_assignment_snapshot=lambda value: self.events.append("snapshot.add"),
            save_execution=lambda value, tenant_id: self.events.append("execution.save"),
        )
        self.runs = SimpleNamespace(save=lambda value: self.events.append("run.save"))

    def __enter__(self):
        self.events.append("uow.enter")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.events.append("uow.exit")
        return False

    def commit(self) -> None:
        self.events.append("commit")


class _Locker:
    def __init__(self, aggregate: CoordinatedRuntimeAggregate) -> None:
        self.aggregate = aggregate
        self.calls = 0

    def lock(self, uow, *, tenant_id, task_id):
        self.calls += 1
        uow.events.append("aggregate.lock")
        return self.aggregate


def _aggregate(
    task: Task,
    subtask: Subtask,
    run: TaskRun,
    attempt: TaskAttempt,
    version: RuntimeVersion,
    *,
    execution=(),
    snapshots=(),
    drain=None,
    boundary=CoordinationRuntimeBoundary.NOT_CROSSED_NO_EXECUTION,
) -> CoordinatedRuntimeAggregate:
    return CoordinatedRuntimeAggregate(
        task=task,
        active_drain=drain,
        cohort=AuthorityCohort("managed", version.id, "off", task.id, task.tenant_id),
        runtime_versions=MappingProxyType({version.id: version}),
        subtasks=(subtask,),
        runs=(run,),
        latest_attempts=MappingProxyType({run.id: attempt}),
        executions=tuple(execution),
        assignment_snapshots=tuple(snapshots),
        handle_snapshots=(),
        lifecycle_operations=(),
        integrity_incidents=(),
        boundary_classifications=MappingProxyType({run.id: boundary}),
    )


def _prepared_chain():
    now, task, subtask, run, attempt, version = _chain()
    assignment = _assignment(task, run, version)
    execution = RuntimeExecution.prepare(
        tenant_id=task.tenant_id,
        run_id=run.id,
        runtime_version_id=version.id,
        assignment_id=UUID(assignment.assignment_id),
        assignment_digest=assignment.assignment_digest or "",
        dispatch_key=f"runtime-dispatch:{task.tenant_id}:{run.runtime_execution_intent_id}",
        dispatch_digest=canonical_digest(
            {
                "execution_id": str(run.runtime_execution_intent_id),
                "dispatch_key": (
                    f"runtime-dispatch:{task.tenant_id}:{run.runtime_execution_intent_id}"
                ),
                "assignment_digest": assignment.assignment_digest,
            }
        ),
        execution_id=run.runtime_execution_intent_id,
        now=now,
    ).claim(
        attempt_id=attempt.id,
        fencing_token=attempt.fencing_token,
        expected_owner_attempt_id=None,
        expected_fencing_token=None,
        expected_version=1,
        now=now,
    )
    run.bind_runtime_execution(execution.id)
    snapshot = assignment_snapshot_for(
        assignment,
        tenant_id=task.tenant_id,
        runtime_execution_id=execution.id,
        created_at=now,
    )
    return now, task, subtask, run, attempt, version, assignment, execution, snapshot


def test_prepare_calls_aggregate_lock_first_and_writes_one_transaction() -> None:
    now, task, subtask, run, attempt, version = _chain()
    assignment = _assignment(task, run, version)
    aggregate = _aggregate(task, subtask, run, attempt, version)
    uow = _Uow()
    locker = _Locker(aggregate)
    result = CoordinatedRuntimeDispatchService(
        uow_factory=lambda: uow, aggregate_locker=locker
    ).prepare_runtime_assignment(
        tenant_id=task.tenant_id,
        task_id=task.id,
        run_id=run.id,
        attempt_id=attempt.id,
        fencing_token=attempt.fencing_token,
        assignment=assignment,
        now=now,
    )
    assert result.kind is CoordinatedRuntimePrepareKind.PREPARED
    assert result.execution_id == run.runtime_execution_intent_id
    assert locker.calls == 1
    assert uow.events == [
        "uow.enter",
        "aggregate.lock",
        "execution.add",
        "run.save",
        "snapshot.add",
        "commit",
        "uow.exit",
    ]


def test_active_drain_returns_identity_without_writes() -> None:
    now, task, subtask, run, attempt, version = _chain()
    drain = SimpleNamespace(id=uuid4(), version=4)
    aggregate = _aggregate(task, subtask, run, attempt, version, drain=drain)
    uow = _Uow()
    result = CoordinatedRuntimeDispatchService(
        uow_factory=lambda: uow, aggregate_locker=_Locker(aggregate)
    ).prepare_runtime_assignment(
        tenant_id=task.tenant_id,
        task_id=task.id,
        run_id=run.id,
        attempt_id=attempt.id,
        fencing_token=attempt.fencing_token,
        assignment=_assignment(task, run, version),
        now=now,
    )
    assert result.kind is CoordinatedRuntimePrepareKind.BLOCKED_BY_DRAIN
    assert (result.drain_id, result.drain_version) == (drain.id, drain.version)
    assert uow.events == ["uow.enter", "aggregate.lock", "uow.exit"]


def test_exact_replay_has_no_writes_or_commit() -> None:
    now, task, subtask, run, attempt, version = _chain()
    assignment = _assignment(task, run, version)
    execution = RuntimeExecution.prepare(
        tenant_id=task.tenant_id,
        run_id=run.id,
        runtime_version_id=version.id,
            assignment_id=UUID(assignment.assignment_id),
        assignment_digest=assignment.assignment_digest or "",
        dispatch_key=f"runtime-dispatch:{task.tenant_id}:{run.runtime_execution_intent_id}",
        dispatch_digest=canonical_digest(
            {
                "execution_id": str(run.runtime_execution_intent_id),
                "dispatch_key": (
                    f"runtime-dispatch:{task.tenant_id}:{run.runtime_execution_intent_id}"
                ),
                "assignment_digest": assignment.assignment_digest,
            }
        ),
        execution_id=run.runtime_execution_intent_id,
        now=now,
    ).claim(
        attempt_id=attempt.id,
        fencing_token=attempt.fencing_token,
        expected_owner_attempt_id=None,
        expected_fencing_token=None,
        expected_version=1,
        now=now,
    )
    snapshot = assignment_snapshot_for(
        assignment,
        tenant_id=task.tenant_id,
        runtime_execution_id=execution.id,
        created_at=now,
    )
    run.bind_runtime_execution(execution.id)
    aggregate = _aggregate(
        task,
        subtask,
        run,
        attempt,
        version,
        execution=(execution,),
        snapshots=(snapshot,),
        boundary=CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED,
    )
    uow = _Uow()
    result = CoordinatedRuntimeDispatchService(
        uow_factory=lambda: uow, aggregate_locker=_Locker(aggregate)
    ).prepare_runtime_assignment(
        tenant_id=task.tenant_id,
        task_id=task.id,
        run_id=run.id,
        attempt_id=attempt.id,
        fencing_token=attempt.fencing_token,
        assignment=assignment,
        now=now,
    )
    assert result.kind is CoordinatedRuntimePrepareKind.REPLAY
    assert result.execution_id == execution.id
    assert uow.events == ["uow.enter", "aggregate.lock", "uow.exit"]


def test_dispatch_boundary_authorizes_one_atomic_cas() -> None:
    now, task, subtask, run, attempt, version, assignment, execution, snapshot = (
        _prepared_chain()
    )
    aggregate = _aggregate(
        task,
        subtask,
        run,
        attempt,
        version,
        execution=(execution,),
        snapshots=(snapshot,),
        boundary=CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED,
    )
    uow = _Uow()
    result = CoordinatedRuntimeDispatchService(
        uow_factory=lambda: uow, aggregate_locker=_Locker(aggregate)
    ).cross_runtime_dispatch_boundary(
        tenant_id=task.tenant_id,
        task_id=task.id,
        run_id=run.id,
        attempt_id=attempt.id,
        fencing_token=attempt.fencing_token,
        runtime_execution_id=execution.id,
        assignment_digest=assignment.assignment_digest or "",
        now=now,
    )
    assert result.kind is CoordinatedRuntimeDispatchKind.DISPATCH_AUTHORIZED
    assert result.execution_id == execution.id
    assert uow.events == ["uow.enter", "aggregate.lock", "execution.save", "commit", "uow.exit"]


def test_dispatch_boundary_replays_crossed_response_loss_without_writes() -> None:
    now, task, subtask, run, attempt, version, assignment, execution, snapshot = (
        _prepared_chain()
    )
    crossed = execution.apply_observation(
        phase=execution.phase.DISPATCHING,
        provider_sequence=None,
        now=now,
    )
    aggregate = _aggregate(
        task,
        subtask,
        run,
        attempt,
        version,
        execution=(crossed,),
        snapshots=(snapshot,),
        boundary=CoordinationRuntimeBoundary.CROSSED_ACTIVE,
    )
    uow = _Uow()
    result = CoordinatedRuntimeDispatchService(
        uow_factory=lambda: uow, aggregate_locker=_Locker(aggregate)
    ).cross_runtime_dispatch_boundary(
        tenant_id=task.tenant_id,
        task_id=task.id,
        run_id=run.id,
        attempt_id=attempt.id,
        fencing_token=attempt.fencing_token,
        runtime_execution_id=execution.id,
        assignment_digest=assignment.assignment_digest or "",
        now=now + timedelta(minutes=1),
    )
    assert result.kind is CoordinatedRuntimeDispatchKind.ALREADY_CROSSED
    assert uow.events == ["uow.enter", "aggregate.lock", "uow.exit"]


def test_dispatch_boundary_drain_blocks_prepared_and_stale_identity_conflicts() -> None:
    now, task, subtask, run, attempt, version, assignment, execution, snapshot = (
        _prepared_chain()
    )
    drain = SimpleNamespace(id=uuid4(), version=3)
    aggregate = _aggregate(
        task,
        subtask,
        run,
        attempt,
        version,
        execution=(execution,),
        snapshots=(snapshot,),
        drain=drain,
        boundary=CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED,
    )
    uow = _Uow()
    result = CoordinatedRuntimeDispatchService(
        uow_factory=lambda: uow, aggregate_locker=_Locker(aggregate)
    ).cross_runtime_dispatch_boundary(
        tenant_id=task.tenant_id,
        task_id=task.id,
        run_id=run.id,
        attempt_id=attempt.id,
        fencing_token=attempt.fencing_token,
        runtime_execution_id=execution.id,
        assignment_digest=assignment.assignment_digest or "",
        now=now,
    )
    assert result.kind is CoordinatedRuntimeDispatchKind.BLOCKED_BY_DRAIN
    assert uow.events == ["uow.enter", "aggregate.lock", "uow.exit"]
    with pytest.raises((InvalidTaskTransition, RuntimeExecutionConflict)):
        CoordinatedRuntimeDispatchService(
            uow_factory=lambda: _Uow(), aggregate_locker=_Locker(aggregate)
        ).cross_runtime_dispatch_boundary(
            tenant_id=task.tenant_id,
            task_id=task.id,
            run_id=run.id,
            attempt_id=attempt.id,
            fencing_token=attempt.fencing_token + 1,
            runtime_execution_id=execution.id,
            assignment_digest=assignment.assignment_digest or "",
            now=now,
        )


@pytest.mark.parametrize("bad", ["task", "run", "attempt", "fence", "lease", "descriptor"])
def test_validation_failures_happen_before_any_write(bad: str) -> None:
    now, task, subtask, run, attempt, version = _chain()
    assignment = _assignment(task, run, version)
    if bad == "task":
        task.status = task.status.COMPLETED
    elif bad == "run":
        run.status = run.status.RECONCILIATION_REQUIRED
    elif bad == "attempt":
        attempt.status = attempt.status.FAILED
    elif bad == "fence":
        attempt = replace(attempt, fencing_token=attempt.fencing_token + 1)
    elif bad == "lease":
        attempt = replace(attempt, lease_expires_at=now - timedelta(seconds=1))
    else:
        assignment = replace(
            assignment, runtime_descriptor_digest="c" * 64, assignment_digest=None
        )
    aggregate = _aggregate(task, subtask, run, attempt, version)
    uow = _Uow()
    with pytest.raises((InvalidTaskTransition, RuntimeExecutionConflict)):
        CoordinatedRuntimeDispatchService(
            uow_factory=lambda: uow, aggregate_locker=_Locker(aggregate)
        ).prepare_runtime_assignment(
            tenant_id=task.tenant_id,
            task_id=task.id,
            run_id=run.id,
            attempt_id=attempt.id,
            fencing_token=7,
            assignment=assignment,
            now=now,
        )
    assert uow.events[:2] == ["uow.enter", "aggregate.lock"]
    assert not any(
        value in uow.events
        for value in {"execution.add", "run.save", "snapshot.add", "commit"}
    )


def test_prepare_command_has_no_admission_registry_or_adapter_callers() -> None:
    root = Path(__file__).parents[1] / "src" / "agentmesh"
    production_calls: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            name = function.attr if isinstance(function, ast.Attribute) else (
                function.id if isinstance(function, ast.Name) else None
            )
            if name in {
                "prepare_runtime_assignment",
                "cross_runtime_dispatch_boundary",
            }:
                production_calls.append(f"{path}:{node.lineno}:{name}")
    assert production_calls == []

    source = root / "application" / "coordinated_runtime_dispatch.py"
    dispatch_tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    forbidden: list[str] = []
    for node in ast.walk(dispatch_tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        name = function.attr if isinstance(function, ast.Attribute) else (
            function.id if isinstance(function, ast.Name) else None
        )
        if name in {
            "prepare_execution_in_uow",
            "validate",
            "dispatch",
            "inspect",
            "execute",
        }:
            forbidden.append(f"{node.lineno}:{name}")
    assert forbidden == []
