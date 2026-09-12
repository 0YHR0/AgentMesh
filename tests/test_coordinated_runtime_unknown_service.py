from __future__ import annotations

import ast
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta, timezone
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from agentmesh.application.coordinated_runtime_unknown import (
    CoordinatedRuntimeUnknownOutcomeService,
    CoordinatedUnknownOutcomeKind,
)
from agentmesh.application.runtime_snapshots import assignment_snapshot_for
from agentmesh.domain.coordination import (
    CoordinationRuntimeBoundary,
    CoordinationRuntimeDrain,
    CoordinationRuntimeDrainStatus,
    CoordinationRuntimeDrainTarget,
    SubtaskStatus,
)
from agentmesh.domain.errors import (
    InvalidTaskInput,
    InvalidTaskTransition,
    RuntimeExecutionConflict,
)
from agentmesh.domain.runtime_execution import RuntimeExecutionPhase
from agentmesh.domain.tasks import AttemptStatus, RunRole, RunStatus
from agentmesh.runtime_sdk import RuntimeObservation, RuntimePhase
from agentmesh.runtime_sdk.assignment import RuntimeAssignment
from agentmesh.runtime_sdk.canonical import thaw_json
from agentmesh.runtime_sdk.descriptor import ArtifactRef, RuntimeDescriptor
from tests.test_coordinated_runtime_barrier import _aggregate

UTC = timezone.utc


def test_unknown_service_c2e2_control_plane_boundary_is_frozen() -> None:
    source_root = Path(__file__).parents[1] / "src" / "agentmesh"
    service_path = source_root / "application" / "coordinated_runtime_unknown.py"
    tree = ast.parse(service_path.read_text(encoding="utf-8"), filename=str(service_path))

    forbidden_import_tokens = {"adapter", "memory", "research", "scheduler"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        else:
            modules = []
        assert not any(
            any(token in module.lower().split(".") for token in forbidden_import_tokens)
            for module in modules
        )

    forbidden_call_names = {
        "dispatch",
        "enqueue",
        "memory",
        "research",
        "redispatch",
        "schedule",
    }
    forbidden_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr.lower() in forbidden_call_names
    ]
    assert forbidden_calls == []
    assert sum(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "commit"
        for node in ast.walk(tree)
    ) == 1
    assert sum(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_uow_factory"
        for node in ast.walk(tree)
    ) == 1

    production_callers = []
    for path in source_root.rglob("*.py"):
        if path == service_path:
            continue
        other_tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.Name)
            and node.id == "CoordinatedRuntimeUnknownOutcomeService"
            for node in ast.walk(other_tree)
        ):
            production_callers.append(str(path))
    assert production_callers == []


class _Uow:
    def __init__(self, aggregate, *, fail_at=None):
        self.aggregate = aggregate
        self.fail_at = fail_at
        self.observations = []
        self.outbox_values = []
        self.lifecycle_values = []
        self.reservations = []
        self.terminal_phases = set()
        self.commits = 0
        self.tasks = SimpleNamespace(save=lambda value: self._write("task_save"))
        self.runs = SimpleNamespace(save=lambda value: self._write("run_save"))
        self.subtasks = SimpleNamespace(save=lambda value: self._write("subtask_save"))
        self.attempts = SimpleNamespace(save=lambda value: self._write("attempt_save"))
        self.coordination_runtime_drains = SimpleNamespace(
            get=self._drain_get,
            add=lambda value: self._set_drain("drain_add", value),
            save=lambda value, tenant_id: self._set_drain("drain_save", value),
        )
        self.quotas = SimpleNamespace(
            list_reservations_for_attempt=lambda attempt_id, for_update=False: list(
                self.reservations
            ),
            save_reservation=lambda value: self._write("quota_save"),
        )
        self.runtimes = SimpleNamespace(
            find_observations=lambda execution_id, tenant_id, limit, offset: list(
                self.observations
            ),
            prior_observations=lambda execution_id, tenant_id, observation_id, digest: list(
                self.observations
            ),
            accepted_terminal_observations=lambda execution_id, tenant_id, phase: (
                [object()] if phase in self.terminal_phases else []
            ),
            add_observation=lambda value: self._add_observation(value),
            add_lifecycle_operation=lambda value: self._add_lifecycle(value),
            save_execution=self._save_execution,
        )
        self.outbox = SimpleNamespace(
            add=lambda value: self._add_outbox(value),
            get=self._outbox_get,
        )
        self.drain = None
        self._snapshot = None

    def _write(self, name):
        self._maybe_fail(name)

    def _maybe_fail(self, name):
        if self.fail_at == name:
            raise RuntimeError(f"fake writer failure: {name}")

    def _add_observation(self, value):
        self._maybe_fail("evidence_add")
        self.observations.append(value)

    def _add_lifecycle(self, value):
        self._maybe_fail("lifecycle_add")
        self.lifecycle_values.append(value)

    def _add_outbox(self, value):
        self._maybe_fail("outbox_add")
        self.outbox_values.append(value)

    def _set_drain(self, name, value):
        self._maybe_fail(name)
        self.drain = value

    def _drain_get(self, drain_id, *, tenant_id, for_update=False):
        return None

    def _outbox_get(self, message_id, *, tenant_id):
        return next(
            (
                value
                for value in self.outbox_values
                if value.message_id == message_id and value.tenant_id == tenant_id
            ),
            None,
        )

    def _save_execution(self, value, tenant_id):
        self._maybe_fail("execution_save")
        self.aggregate = replace(
            self.aggregate,
            executions=tuple(
                value if current.id == value.id else current
                for current in self.aggregate.executions
            ),
        )

    def __enter__(self):
        self._snapshot = {
            "aggregate": replace(
                self.aggregate,
                task=deepcopy(self.aggregate.task),
                active_drain=deepcopy(self.aggregate.active_drain),
                subtasks=deepcopy(self.aggregate.subtasks),
                runs=deepcopy(self.aggregate.runs),
                latest_attempts=MappingProxyType(
                    {
                        key: deepcopy(value)
                        for key, value in self.aggregate.latest_attempts.items()
                    }
                ),
                executions=deepcopy(self.aggregate.executions),
            ),
            "observations": list(self.observations),
            "outbox_values": list(self.outbox_values),
            "lifecycle_values": list(self.lifecycle_values),
            "reservations": deepcopy(self.reservations),
            "terminal_phases": deepcopy(self.terminal_phases),
            "drain": deepcopy(self.drain),
        }
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is not None and self._snapshot is not None:
            self.aggregate = self._snapshot["aggregate"]
            self.observations[:] = self._snapshot["observations"]
            self.outbox_values[:] = self._snapshot["outbox_values"]
            self.lifecycle_values[:] = self._snapshot["lifecycle_values"]
            self.reservations[:] = self._snapshot["reservations"]
            self.terminal_phases = self._snapshot["terminal_phases"]
            self.drain = self._snapshot["drain"]
        return False

    def commit(self):
        self._maybe_fail("commit")
        self.commits += 1


class _Locker:
    def __init__(self, aggregate):
        self.aggregate = aggregate
        self.calls = 0

    def lock(self, uow, *, tenant_id, task_id):
        self.calls += 1
        return uow.aggregate if uow.fail_at is not None else self.aggregate


def _observation(target, now, *, phase=RuntimePhase.OUTCOME_UNKNOWN, **changes):
    execution = target[3]
    observation = RuntimeObservation(
        observation_id=str(uuid4()),
        runtime_execution_id=str(execution.id),
        assignment_id=str(execution.assignment_id),
        assignment_digest=execution.assignment_digest,
        phase=phase,
        observed_at=now,
        provider_event_id="unknown-service-test",
        provider_sequence=2,
    )
    for name, value in changes.items():
        object.__setattr__(observation, name, value)
    return observation


def _real_snapshot_aggregate():
    """Build the locked shape with an immutable, identity-complete snapshot."""
    task, target, aggregate = _aggregate()
    run = target[1]
    execution = target[3]
    agent_version_id = uuid4()
    run.agent_version_id = agent_version_id
    run.agent_version_digest = "c" * 64
    assignment = RuntimeAssignment(
        assignment_id=str(execution.assignment_id),
        tenant_id=task.tenant_id,
        task_id=str(task.id),
        run_id=str(run.id),
        agent_definition_id=str(uuid4()),
        agent_version_id=str(agent_version_id),
        agent_version_digest=run.agent_version_digest,
        runtime_version_id=str(run.runtime_version_id),
        runtime_descriptor_digest=RuntimeDescriptor.from_dict(
            thaw_json(aggregate.runtime_versions[run.runtime_version_id].descriptor)
        ).digest(),
        execution_mode="managed_async",
        run_role="EXECUTOR",
        revision=0,
        objective="unknown outcome test",
        correlation_ids={"runtime_execution_id": str(execution.id)},
    )
    execution = replace(execution, assignment_digest=assignment.assignment_digest)
    snapshot = assignment_snapshot_for(
        assignment,
        tenant_id=task.tenant_id,
        runtime_execution_id=execution.id,
        created_at=execution.created_at,
    )
    aggregate = replace(
        aggregate,
        executions=(execution,),
        assignment_snapshots=(snapshot,),
    )
    target = (target[0], run, target[2], execution)
    return task, target, aggregate


def _real_supervisor_snapshot_aggregate():
    task, target, aggregate = _real_snapshot_aggregate()
    subtask, executor_run, attempt, execution = target
    supervisor_run = replace(executor_run, role=RunRole.SUPERVISOR, subtask_id=None)
    terminal_subtask = replace(subtask, status=SubtaskStatus.COMPLETED, current_run_id=None)
    task.current_run_id = supervisor_run.id
    aggregate = replace(
        aggregate,
        task=task,
        subtasks=(terminal_subtask,),
        runs=(supervisor_run,),
        boundary_classifications=MappingProxyType({}),
    )
    return task, (terminal_subtask, supervisor_run, attempt, execution), aggregate


def _real_stopping_sibling_aggregate(drain_target):
    task, target, aggregate = _aggregate(sibling_count=1, drain_target=drain_target)
    aggregate = replace(
        aggregate,
        active_drain=replace(
            aggregate.active_drain,
            id=uuid5(NAMESPACE_URL, f"coordination-runtime-drain:{task.tenant_id}:{task.id}"),
        ),
    )
    run = target[1]
    execution = target[3]
    agent_version_id = uuid4()
    run.agent_version_id = agent_version_id
    run.agent_version_digest = "c" * 64
    assignment = RuntimeAssignment(
        assignment_id=str(execution.assignment_id),
        tenant_id=task.tenant_id,
        task_id=str(task.id),
        run_id=str(run.id),
        agent_definition_id=str(uuid4()),
        agent_version_id=str(agent_version_id),
        agent_version_digest=run.agent_version_digest,
        runtime_version_id=str(run.runtime_version_id),
        runtime_descriptor_digest=RuntimeDescriptor.from_dict(
            thaw_json(aggregate.runtime_versions[run.runtime_version_id].descriptor)
        ).digest(),
        execution_mode="managed_async",
        run_role="EXECUTOR",
        revision=0,
        objective="unknown outcome sibling test",
        correlation_ids={"runtime_execution_id": str(execution.id)},
    )
    execution = replace(execution, assignment_digest=assignment.assignment_digest)
    snapshot = assignment_snapshot_for(
        assignment,
        tenant_id=task.tenant_id,
        runtime_execution_id=execution.id,
        created_at=execution.created_at,
    )
    aggregate = replace(
        aggregate,
        executions=tuple(
            execution if value.id == execution.id else value for value in aggregate.executions
        ),
        assignment_snapshots=(snapshot,),
    )
    target = (target[0], run, target[2], execution)
    return task, target, aggregate


@pytest.mark.parametrize("phase", [RuntimePhase.LOST, RuntimePhase.OUTCOME_UNKNOWN])
def test_unknown_service_real_assignment_path_first_and_exact_replay(phase):
    task, target, aggregate = _real_snapshot_aggregate()
    uow = _Uow(aggregate)
    locker = _Locker(aggregate)
    service = CoordinatedRuntimeUnknownOutcomeService(
        uow_factory=lambda: uow,
        cancel_deadline_window=timedelta(minutes=5),
        aggregate_locker=locker,
    )
    now = target[3].updated_at + timedelta(seconds=10)
    observation = _observation(target, now, phase=phase)
    kwargs = dict(
        tenant_id=task.tenant_id,
        task_id=task.id,
        run_id=target[1].id,
        attempt_id=target[2].id,
        fencing_token=target[2].fencing_token,
        runtime_execution_id=target[3].id,
        observation=observation,
        received_at=now,
        causation_id=uuid4(),
    )
    first = service.park_unknown(**kwargs)
    locker.aggregate = replace(
        aggregate,
        active_drain=uow.drain,
        executions=tuple(uow.aggregate.executions),
        boundary_classifications=MappingProxyType(
            {target[1].id: CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE}
        ),
    )
    replay = service.park_unknown(**kwargs)
    assert first.kind is CoordinatedUnknownOutcomeKind.PARKED
    assert replay.kind is CoordinatedUnknownOutcomeKind.REPLAY
    assert replay.observation_digest == first.observation_digest
    assert uow.commits == 1
    assert len(uow.observations) == 1
    assert len(uow.outbox_values) == 1


@pytest.mark.parametrize("phase", [RuntimePhase.LOST, RuntimePhase.OUTCOME_UNKNOWN])
def test_unknown_service_supervisor_first_and_exact_replay(phase):
    task, target, aggregate = _real_supervisor_snapshot_aggregate()
    uow = _Uow(aggregate)
    locker = _Locker(aggregate)
    service = CoordinatedRuntimeUnknownOutcomeService(
        uow_factory=lambda: uow,
        cancel_deadline_window=timedelta(minutes=5),
        aggregate_locker=locker,
    )
    now = target[3].updated_at + timedelta(seconds=10)
    observation = _observation(target, now, phase=phase)
    kwargs = dict(
        tenant_id=task.tenant_id,
        task_id=task.id,
        run_id=target[1].id,
        attempt_id=target[2].id,
        fencing_token=target[2].fencing_token,
        runtime_execution_id=target[3].id,
        observation=observation,
        received_at=now,
        causation_id=uuid4(),
    )
    first = service.park_unknown(**kwargs)
    locker.aggregate = replace(
        aggregate,
        active_drain=uow.drain,
        executions=tuple(uow.aggregate.executions),
        boundary_classifications=MappingProxyType({}),
    )
    replay = service.park_unknown(**kwargs)
    assert first.kind is CoordinatedUnknownOutcomeKind.PARKED
    assert replay.kind is CoordinatedUnknownOutcomeKind.REPLAY
    assert first.subtask_status is None
    assert replay.subtask_status is None
    assert uow.commits == 1


@pytest.mark.parametrize(
    "drain_target",
    [
        CoordinationRuntimeDrainTarget.WAITING_APPROVAL,
        CoordinationRuntimeDrainTarget.FAILED,
        CoordinationRuntimeDrainTarget.CANCELED,
    ],
)
def test_unknown_service_stopping_sibling_drain_has_stable_cancel_replay(drain_target):
    task, target, aggregate = _real_stopping_sibling_aggregate(drain_target)
    uow = _Uow(aggregate)
    locker = _Locker(aggregate)
    service = CoordinatedRuntimeUnknownOutcomeService(
        uow_factory=lambda: uow,
        cancel_deadline_window=timedelta(minutes=5),
        aggregate_locker=locker,
    )
    now = target[3].updated_at + timedelta(seconds=10)
    observation = _observation(target, now)
    kwargs = dict(
        tenant_id=task.tenant_id,
        task_id=task.id,
        run_id=target[1].id,
        attempt_id=target[2].id,
        fencing_token=target[2].fencing_token,
        runtime_execution_id=target[3].id,
        observation=observation,
        received_at=now,
        causation_id=uuid4(),
    )
    first = service.park_unknown(**kwargs)
    assert first.kind is CoordinatedUnknownOutcomeKind.DRAINING_ACTIVE
    assert first.drain_target is drain_target
    assert first.lifecycle_operation_ids
    locker.aggregate = replace(
        aggregate,
        active_drain=aggregate.active_drain,
        executions=tuple(uow.aggregate.executions),
        lifecycle_operations=tuple(uow.lifecycle_values),
        boundary_classifications=aggregate.boundary_classifications,
    )
    replay = service.park_unknown(**kwargs)
    assert replay.kind is CoordinatedUnknownOutcomeKind.REPLAY
    assert replay.lifecycle_operation_ids == first.lifecycle_operation_ids
    assert uow.commits == 1


def _executor_replay_case():
    task, target, aggregate = _real_snapshot_aggregate()
    uow = _Uow(aggregate)
    locker = _Locker(aggregate)
    service = CoordinatedRuntimeUnknownOutcomeService(
        uow_factory=lambda: uow,
        cancel_deadline_window=timedelta(minutes=5),
        aggregate_locker=locker,
    )
    now = target[3].updated_at + timedelta(seconds=10)
    observation = _observation(target, now)
    kwargs = dict(
        tenant_id=task.tenant_id,
        task_id=task.id,
        run_id=target[1].id,
        attempt_id=target[2].id,
        fencing_token=target[2].fencing_token,
        runtime_execution_id=target[3].id,
        observation=observation,
        received_at=now,
        causation_id=uuid4(),
    )
    service.park_unknown(**kwargs)
    locker.aggregate = replace(
        aggregate,
        active_drain=uow.drain,
        executions=tuple(uow.aggregate.executions),
        boundary_classifications=MappingProxyType(
            {target[1].id: CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE}
        ),
    )
    return task, target, aggregate, uow, locker, service, kwargs


@pytest.mark.parametrize(
    "label",
    [
        "evidence_field",
        "evidence_digest",
        "runtime_phase",
        "runtime_sequence",
        "runtime_owner",
        "attempt_status",
        "attempt_settlement",
        "run",
        "executor_subtask",
        "task_error",
        "task_output",
        "task_candidate",
        "drain_id",
        "drain_target",
        "drain_reason",
        "drain_status",
        "outbox_missing",
        "outbox_payload",
        "quota_unreleased",
        "later_known_terminal",
    ],
)
def test_unknown_service_replay_partial_projection_is_read_only(label):
    task, target, aggregate, uow, locker, service, kwargs = _executor_replay_case()
    if label == "evidence_field":
        uow.observations[0] = replace(uow.observations[0], assignment_id=uuid4())
    elif label == "evidence_digest":
        uow.observations[0] = replace(uow.observations[0], observation_digest="d" * 64)
    elif label == "runtime_phase":
        execution = replace(aggregate.executions[0], phase=RuntimeExecutionPhase.RUNNING)
        locker.aggregate = replace(locker.aggregate, executions=(execution,))
    elif label == "runtime_sequence":
        execution = replace(
            aggregate.executions[0], provider_sequence=aggregate.executions[0].provider_sequence + 1
        )
        locker.aggregate = replace(locker.aggregate, executions=(execution,))
    elif label == "runtime_owner":
        execution = replace(aggregate.executions[0], current_owner_attempt_id=uuid4())
        locker.aggregate = replace(locker.aggregate, executions=(execution,))
    elif label == "attempt_status":
        attempt = replace(target[2], status=AttemptStatus.RUNNING)
        locker.aggregate = replace(
            locker.aggregate,
            latest_attempts=MappingProxyType({target[1].id: attempt}),
        )
    elif label == "attempt_settlement":
        attempt = replace(target[2], settled_tokens=1, settled_cost_micros=1)
        locker.aggregate = replace(
            locker.aggregate,
            latest_attempts=MappingProxyType({target[1].id: attempt}),
        )
    elif label == "run":
        locker.aggregate = replace(
            locker.aggregate,
            runs=(replace(target[1], status=RunStatus.RUNNING),),
        )
    elif label == "executor_subtask":
        locker.aggregate = replace(
            locker.aggregate,
            subtasks=(replace(target[0], status=SubtaskStatus.RUNNING),),
        )
    elif label == "task_error":
        locker.aggregate = replace(locker.aggregate, task=replace(task, error="tampered"))
    elif label == "task_output":
        locker.aggregate = replace(locker.aggregate, task=replace(task, output={"tampered": True}))
    elif label == "task_candidate":
        locker.aggregate = replace(
            locker.aggregate, task=replace(task, candidate_output={"tampered": True})
        )
    elif label == "drain_id":
        locker.aggregate = replace(
            locker.aggregate,
            active_drain=replace(locker.aggregate.active_drain, id=uuid4()),
        )
    elif label == "drain_target":
        locker.aggregate = replace(
            locker.aggregate,
            active_drain=replace(
                locker.aggregate.active_drain,
                target=CoordinationRuntimeDrainTarget.WAITING_APPROVAL,
            ),
        )
    elif label == "drain_reason":
        locker.aggregate = replace(
            locker.aggregate,
            active_drain=replace(locker.aggregate.active_drain, reason="tampered"),
        )
    elif label == "drain_status":
        locker.aggregate = replace(
            locker.aggregate,
            active_drain=replace(
                locker.aggregate.active_drain,
                status=CoordinationRuntimeDrainStatus.COMPLETE,
                completed_at=locker.aggregate.active_drain.updated_at,
            ),
        )
    elif label == "outbox_missing":
        uow.outbox_values.clear()
    elif label == "outbox_payload":
        uow.outbox_values[0] = replace(
            uow.outbox_values[0], payload={"tampered": True}
        )
    elif label == "quota_unreleased":
        uow.reservations.append(SimpleNamespace(released_at=None))
    elif label == "later_known_terminal":
        uow.terminal_phases.add(RuntimeExecutionPhase.SUCCEEDED)
    with pytest.raises(RuntimeExecutionConflict):
        service.park_unknown(**kwargs)
    assert uow.commits == 1
    assert len(uow.observations) == 1
    assert len(uow.outbox_values) in {0, 1}


@pytest.mark.parametrize("label", ["lifecycle_missing", "lifecycle_extra", "lifecycle_conflict"])
def test_unknown_service_stopping_replay_lifecycle_projection_is_read_only(label):
    task, target, aggregate = _real_stopping_sibling_aggregate(
        CoordinationRuntimeDrainTarget.WAITING_APPROVAL
    )
    uow = _Uow(aggregate)
    locker = _Locker(aggregate)
    service = CoordinatedRuntimeUnknownOutcomeService(
        uow_factory=lambda: uow,
        cancel_deadline_window=timedelta(minutes=5),
        aggregate_locker=locker,
    )
    now = target[3].updated_at + timedelta(seconds=10)
    observation = _observation(target, now)
    kwargs = dict(
        tenant_id=task.tenant_id,
        task_id=task.id,
        run_id=target[1].id,
        attempt_id=target[2].id,
        fencing_token=target[2].fencing_token,
        runtime_execution_id=target[3].id,
        observation=observation,
        received_at=now,
        causation_id=uuid4(),
    )
    service.park_unknown(**kwargs)
    locker.aggregate = replace(
        aggregate,
        active_drain=aggregate.active_drain,
        executions=tuple(uow.aggregate.executions),
        lifecycle_operations=tuple(uow.lifecycle_values),
        boundary_classifications=aggregate.boundary_classifications,
    )
    row = uow.lifecycle_values[0]
    if label == "lifecycle_missing":
        locker.aggregate = replace(locker.aggregate, lifecycle_operations=())
    elif label == "lifecycle_extra":
        locker.aggregate = replace(
            locker.aggregate, lifecycle_operations=(row, row)
        )
    else:
        locker.aggregate = replace(
            locker.aggregate,
            lifecycle_operations=(replace(row, operation_id="runtime-cancel:conflict:v1"),),
        )
    with pytest.raises(RuntimeExecutionConflict):
        service.park_unknown(**kwargs)
    assert uow.commits == 1
    assert len(uow.observations) == 1


def test_unknown_service_rejects_partial_outbox_before_first_write():
    task, target, aggregate = _real_snapshot_aggregate()
    uow = _Uow(aggregate)
    locker = _Locker(aggregate)
    service = CoordinatedRuntimeUnknownOutcomeService(
        uow_factory=lambda: uow,
        cancel_deadline_window=timedelta(minutes=5),
        aggregate_locker=locker,
    )
    now = target[3].updated_at + timedelta(seconds=10)
    observation = _observation(target, now)
    from agentmesh.application.coordinated_runtime_unknown import (
        _observation_digest,
        _reconciliation_event,
    )

    drain = CoordinationRuntimeDrain.start(
        drain_id=uuid5(NAMESPACE_URL, f"coordination-runtime-drain:{task.tenant_id}:{task.id}"),
        tenant_id=task.tenant_id,
        task_id=task.id,
        triggering_run_id=target[1].id,
        target=CoordinationRuntimeDrainTarget.RUNNING,
        reason="coordination.runtime_reconciliation_required",
        at=now,
    )
    uow.outbox_values.append(
        _reconciliation_event(
            tenant_id=task.tenant_id,
            task_id=task.id,
            run_id=target[1].id,
            attempt_id=target[2].id,
            execution_id=target[3].id,
            observation_id=observation.observation_id,
            digest=_observation_digest(observation),
            causation_id=uuid4(),
            at=now,
            runtime_phase=observation.phase.value,
            drain=drain,
            attempt=target[2],
            task=task,
        )
    )
    with pytest.raises(RuntimeExecutionConflict):
        service.park_unknown(
            tenant_id=task.tenant_id,
            task_id=task.id,
            run_id=target[1].id,
            attempt_id=target[2].id,
            fencing_token=target[2].fencing_token,
            runtime_execution_id=target[3].id,
            observation=observation,
            received_at=now,
            causation_id=uuid4(),
        )
    assert locker.calls == 1
    assert uow.commits == 0
    assert uow.observations == []
    assert len(uow.outbox_values) == 1


def _rollback_signature(uow):
    aggregate = uow.aggregate
    return (
        aggregate.task.status,
        aggregate.task.error,
        aggregate.task.output,
        aggregate.task.candidate_output,
        aggregate.task.current_run_id,
        tuple((run.id, run.status, run.error) for run in aggregate.runs),
        tuple(
            (attempt.id, attempt.status, attempt.error, attempt.settled_tokens)
            for attempt in aggregate.latest_attempts.values()
            if attempt is not None
        ),
        tuple((subtask.id, subtask.status) for subtask in aggregate.subtasks),
        tuple(
            (execution.id, execution.phase, execution.provider_sequence)
            for execution in aggregate.executions
        ),
        aggregate.active_drain,
        tuple(value.observation_id for value in uow.observations),
        tuple(value.message_id for value in uow.outbox_values),
        tuple(value.operation_id for value in uow.lifecycle_values),
        uow.commits,
    )


@pytest.mark.parametrize(
    "fail_at",
    [
        "evidence_add",
        "execution_save",
        "attempt_save",
        "run_save",
        "subtask_save",
        "drain_add",
        "task_save",
        "outbox_add",
        "commit",
    ],
)
def test_unknown_service_writer_failure_rolls_back_every_projection(fail_at):
    task, target, aggregate = _real_snapshot_aggregate()
    uow = _Uow(aggregate, fail_at=fail_at)
    locker = _Locker(aggregate)
    service = CoordinatedRuntimeUnknownOutcomeService(
        uow_factory=lambda: uow,
        cancel_deadline_window=timedelta(minutes=5),
        aggregate_locker=locker,
    )
    now = target[3].updated_at + timedelta(seconds=10)
    before = _rollback_signature(uow)
    with pytest.raises(RuntimeError, match="fake writer failure"):
        service.park_unknown(
            tenant_id=task.tenant_id,
            task_id=task.id,
            run_id=target[1].id,
            attempt_id=target[2].id,
            fencing_token=target[2].fencing_token,
            runtime_execution_id=target[3].id,
            observation=_observation(target, now),
            received_at=now,
            causation_id=uuid4(),
        )
    assert _rollback_signature(uow) == before


def test_unknown_service_lifecycle_writer_failure_rolls_back_sibling_barrier():
    task, target, aggregate = _real_stopping_sibling_aggregate(
        CoordinationRuntimeDrainTarget.WAITING_APPROVAL
    )
    uow = _Uow(aggregate, fail_at="lifecycle_add")
    locker = _Locker(aggregate)
    service = CoordinatedRuntimeUnknownOutcomeService(
        uow_factory=lambda: uow,
        cancel_deadline_window=timedelta(minutes=5),
        aggregate_locker=locker,
    )
    now = target[3].updated_at + timedelta(seconds=10)
    before = _rollback_signature(uow)
    with pytest.raises(RuntimeError, match="fake writer failure"):
        service.park_unknown(
            tenant_id=task.tenant_id,
            task_id=task.id,
            run_id=target[1].id,
            attempt_id=target[2].id,
            fencing_token=target[2].fencing_token,
            runtime_execution_id=target[3].id,
            observation=_observation(target, now),
            received_at=now,
            causation_id=uuid4(),
        )
    assert _rollback_signature(uow) == before


@pytest.mark.parametrize(
    "label",
    [
        "assignment_id",
        "assignment_digest",
        "runtime_descriptor",
        "runtime_version",
        "tenant",
        "run",
        "attempt",
        "fence",
    ],
)
def test_unknown_service_identity_chain_conflict_has_no_writes(label):
    task, target, aggregate = _real_snapshot_aggregate()
    run, attempt, execution = target[1], target[2], target[3]
    command_tenant = task.tenant_id
    command_run = run.id
    command_attempt = attempt.id
    command_fence = attempt.fencing_token
    if label == "assignment_id":
        execution = replace(execution, assignment_id=uuid4())
        aggregate = replace(aggregate, executions=(execution,))
    elif label == "assignment_digest":
        execution = replace(execution, assignment_digest="d" * 64)
        aggregate = replace(aggregate, executions=(execution,))
    elif label == "runtime_descriptor":
        version = replace(
            aggregate.runtime_versions[run.runtime_version_id],
            descriptor=MappingProxyType({}),
        )
        aggregate = replace(
            aggregate,
            runtime_versions=MappingProxyType({run.runtime_version_id: version}),
        )
    elif label == "runtime_version":
        run = replace(run, runtime_version_id=uuid4())
        aggregate = replace(aggregate, runs=(run,))
    elif label == "tenant":
        command_tenant = "another-tenant"
    elif label == "run":
        command_run = uuid4()
    elif label == "attempt":
        command_attempt = uuid4()
    elif label == "fence":
        command_fence += 1
    uow = _Uow(aggregate)
    locker = _Locker(aggregate)
    service = CoordinatedRuntimeUnknownOutcomeService(
        uow_factory=lambda: uow,
        cancel_deadline_window=timedelta(minutes=5),
        aggregate_locker=locker,
    )
    now = execution.updated_at + timedelta(seconds=10)
    observation = _observation(target, now)
    with pytest.raises((RuntimeExecutionConflict, InvalidTaskTransition)):
        service.park_unknown(
            tenant_id=command_tenant,
            task_id=task.id,
            run_id=command_run,
            attempt_id=command_attempt,
            fencing_token=command_fence,
            runtime_execution_id=execution.id,
            observation=observation,
            received_at=now,
            causation_id=uuid4(),
        )
    assert locker.calls == 1
    assert uow.commits == 0
    assert uow.observations == []
    assert uow.outbox_values == []


def test_unknown_service_rejects_wrong_phase_before_lock():
    task, target, aggregate = _aggregate()
    locker = _Locker(aggregate)
    uow = _Uow(aggregate)
    service = CoordinatedRuntimeUnknownOutcomeService(
        uow_factory=lambda: uow,
        cancel_deadline_window=timedelta(minutes=5),
        aggregate_locker=locker,
    )
    now = target[3].updated_at + timedelta(seconds=10)
    observation = _observation(target, now)
    observation = RuntimeObservation.from_dict(
        {**observation.to_dict(), "phase": RuntimePhase.SUCCEEDED.value, "output": {"ok": True}}
    )
    with pytest.raises(InvalidTaskInput):
        service.park_unknown(
            tenant_id=task.tenant_id,
            task_id=task.id,
            run_id=target[1].id,
            attempt_id=target[2].id,
            fencing_token=target[2].fencing_token,
            runtime_execution_id=target[3].id,
            observation=observation,
            received_at=now,
            causation_id=uuid4(),
        )
    assert locker.calls == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("output", {"unexpected": True}),
        (
            "output_artifact_refs",
            (
                ArtifactRef(
                    artifact_id=str(uuid4()), version_id=str(uuid4()), digest="a" * 64
                ),
            ),
        ),
        ("usage", {"tokens": 1}),
        ("governed_action_requests", ({"action": "send"},)),
        ("wait_refs", ("wait-1",)),
    ],
)
def test_unknown_service_rejects_forbidden_shape_before_lock(field, value):
    task, target, aggregate = _aggregate()
    locker = _Locker(aggregate)
    uow = _Uow(aggregate)
    service = CoordinatedRuntimeUnknownOutcomeService(
        uow_factory=lambda: uow,
        cancel_deadline_window=timedelta(minutes=5),
        aggregate_locker=locker,
    )
    now = target[3].updated_at + timedelta(seconds=10)
    observation = _observation(target, now, **{field: value})
    with pytest.raises(InvalidTaskInput):
        service.park_unknown(
            tenant_id=task.tenant_id,
            task_id=task.id,
            run_id=target[1].id,
            attempt_id=target[2].id,
            fencing_token=target[2].fencing_token,
            runtime_execution_id=target[3].id,
            observation=observation,
            received_at=now,
            causation_id=uuid4(),
        )
    assert locker.calls == 0
