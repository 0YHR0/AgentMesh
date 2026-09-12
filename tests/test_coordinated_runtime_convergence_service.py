from __future__ import annotations

import ast
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

from agentmesh.application.coordinated_runtime_barrier import (
    CoordinatedBarrierCompletion,
)
from agentmesh.application.coordinated_runtime_convergence import (
    CoordinatedKnownTerminalKind,
    CoordinatedRuntimeConvergenceService,
    _select_target,
)
from agentmesh.domain.coordination import (
    CoordinationRuntimeBoundary,
    CoordinationRuntimeDrain,
    CoordinationRuntimeDrainTarget,
)
from agentmesh.domain.errors import InvalidTaskTransition, RuntimeExecutionConflict
from agentmesh.domain.runtime_execution import (
    RuntimeExecutionPhase,
    RuntimeObservationEvidence,
    RuntimeObservationOutcome,
)
from agentmesh.domain.tasks import AttemptStatus, TaskExecutionMode, TaskStatus
from agentmesh.runtime_sdk import RuntimeObservation, RuntimePhase
from tests.test_coordinated_runtime_barrier import (
    _aggregate,
    _aggregate_for_sibling_boundaries,
    _cancel_intent,
)

UTC = timezone.utc


def _snapshot_value(value):
    if isinstance(value, MappingProxyType):
        return MappingProxyType({key: _snapshot_value(child) for key, child in value.items()})
    try:
        return deepcopy(value)
    except TypeError:
        if isinstance(value, dict):
            return {key: _snapshot_value(child) for key, child in value.items()}
        if isinstance(value, list):
            return [_snapshot_value(child) for child in value]
        if isinstance(value, tuple):
            return tuple(_snapshot_value(child) for child in value)
        return value


def _snapshot_entity(value):
    return {key: _snapshot_value(child) for key, child in value.__dict__.items()}


def _restore_entity(value, snapshot):
    value.__dict__.clear()
    value.__dict__.update(snapshot)


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
    def __init__(self, aggregate, *, inject_at=None):
        self.aggregate = aggregate
        self.inject_at = inject_at
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

    def maybe_fail(self, point: str) -> None:
        if self.inject_at == point:
            raise RuntimeError(f"injected {point} failure")

    def factory(self):
        return _Uow(self)


class _Uow:
    def __init__(self, state: _State):
        self.state = state
        self._snapshot = None
        self._pending_evidence: list[RuntimeObservationEvidence] = []
        self._pending_executions = []
        self._pending_drain = None
        self._pending_lifecycle = []
        self._pending_outbox = []
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
        self._snapshot = self.state.aggregate
        self._entity_snapshots = [
            (value, _snapshot_entity(value))
            for value in (
                self.state.aggregate.task,
                *self.state.aggregate.subtasks,
                *self.state.aggregate.runs,
                *(value for value in self.state.aggregate.latest_attempts.values() if value),
                *self.state.aggregate.executions,
            )
        ]
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is not None or not self._committed:
            if exc_type is not None:
                self.state.rollbacks += 1
                self.state.operation_log.append("uow.rollback")
            else:
                self.state.operation_log.append("uow.close")
            if exc_type is not None and self._snapshot is not None:
                for value, snapshot in self._entity_snapshots:
                    _restore_entity(value, snapshot)
                self.state.aggregate = self._snapshot
        return False

    def commit(self) -> None:
        self.state.maybe_fail("commit")
        self.state.commits += 1
        self.state.operation_log.append("uow.commit")
        self.state.evidence.extend(self._pending_evidence)
        self.state.lifecycle_count += len(self._pending_lifecycle)
        self.state.outbox_count += len(self._pending_outbox)
        for execution in getattr(self, "_pending_executions", ()):
            self.state.aggregate = replace(
                self.state.aggregate,
                executions=tuple(
                    execution if value.id == execution.id else value
                    for value in self.state.aggregate.executions
                ),
            )
        if getattr(self, "_pending_drain", None) is not None:
            self.state.drain = self._pending_drain
            self.state.aggregate = replace(
                self.state.aggregate, active_drain=self._pending_drain
            )
        self._committed = True

    def _save_task(self, _value) -> None:
        self.state.maybe_fail("task_save")
        self.state.task_saves += 1
        self.state.operation_log.append("tasks.save")

    def _save_run(self, _value) -> None:
        self.state.maybe_fail("run_save")
        self.state.operation_log.append("runs.save")

    def _save_subtask(self, _value) -> None:
        self.state.maybe_fail("subtask_save")
        self.state.operation_log.append("subtasks.save")

    def _save_attempt(self, _value) -> None:
        self.state.maybe_fail("attempt_save")
        self.state.operation_log.append("attempts.save")

    def _get_drain(self, _drain_id, *, tenant_id, for_update=False):
        self.state.operation_log.append("drains.get")
        if self.state.drain is not None and self.state.drain.tenant_id == tenant_id:
            return self.state.drain
        return None

    def _add_drain(self, drain, *, tenant_id):
        self.state.maybe_fail("drain_add")
        self.state.operation_log.append("drains.add")
        self._pending_drain = drain

    def _save_drain(self, drain, *, tenant_id):
        self.state.maybe_fail("drain_save")
        self.state.operation_log.append("drains.save")
        self._pending_drain = drain

    def _list_reservations(self, _attempt_id, *, for_update=False):
        self.state.maybe_fail("quota_release")
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
        self.state.maybe_fail("observation_add")
        self.state.observation_adds += 1
        self.state.operation_log.append("observations.add")
        self._pending_evidence.append(evidence)

    def _save_execution(self, execution, *, tenant_id):
        self.state.maybe_fail("execution_save")
        self.state.execution_saves += 1
        self.state.operation_log.append("executions.save")
        self._pending_executions = [execution]

    def _add_lifecycle(self, _value):
        self._pending_lifecycle.append(_value)
        self.state.operation_log.append("lifecycle.add")

    def _add_outbox(self, _value):
        self.state.maybe_fail("outbox")
        self._pending_outbox.append(_value)
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
            uow._pending_drain = drain
        if plan.completion is CoordinatedBarrierCompletion.WAIT_ACTIVE:
            if any(action.kind.value == "REQUEST_CANCEL" for action in plan.sibling_actions):
                uow.runtimes.add_lifecycle_operation(object())
                uow.outbox.add(object())
                self.state.maybe_fail("barrier")
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


def _durable_fingerprint(state: _State):
    aggregate = state.aggregate
    entities = (
        aggregate.task,
        *aggregate.subtasks,
        *aggregate.runs,
        *(value for value in aggregate.latest_attempts.values() if value),
        *aggregate.executions,
    )
    return (
        tuple(
            tuple(sorted((key, repr(value)) for key, value in entity.__dict__.items()))
            for entity in entities
        ),
        repr(aggregate.active_drain),
        repr(state.drain),
        tuple(repr(value) for value in state.evidence),
        state.lifecycle_count,
        state.outbox_count,
    )


@pytest.mark.parametrize(
    "inject_at",
    [
        "observation_add",
        "execution_save",
        "attempt_save",
        "run_save",
        "subtask_save",
        "quota_release",
        "scheduler",
        "commit",
    ],
)
def test_service_success_injected_failures_restore_durable_state(monkeypatch, inject_at) -> None:
    _task, target, aggregate = _aggregate()
    state, service = _service_state(monkeypatch, aggregate, target)
    state.inject_at = inject_at
    original_schedule = _Scheduler.schedule
    if inject_at == "scheduler":
        def fail_schedule(self, uow, task, *, at, causation_id):
            self.state.maybe_fail("scheduler")
            return original_schedule(self, uow, task, at=at, causation_id=causation_id)

        monkeypatch.setattr(_Scheduler, "schedule", fail_schedule)
    before = _durable_fingerprint(state)
    with pytest.raises(RuntimeError, match="injected"):
        _call(service, target, phase=RuntimePhase.SUCCEEDED, now=_now_for(target))
    assert _durable_fingerprint(state) == before
    assert state.commits == 0
    assert state.rollbacks == 1


@pytest.mark.parametrize(
    "inject_at",
    [
        "observation_add",
        "execution_save",
        "attempt_save",
        "run_save",
        "subtask_save",
        "quota_release",
        "drain_save",
        "task_save",
        "commit",
    ],
)
def test_service_failure_injected_failures_restore_durable_state(monkeypatch, inject_at) -> None:
    _task, target, aggregate = _aggregate()
    state, service = _service_state(monkeypatch, aggregate, target)
    state.inject_at = inject_at
    before = _durable_fingerprint(state)
    with pytest.raises(RuntimeError, match="injected"):
        _call(service, target, phase=RuntimePhase.FAILED, now=_now_for(target))
    assert _durable_fingerprint(state) == before
    assert state.commits == 0
    assert state.rollbacks == 1


@pytest.mark.parametrize("inject_at", ["barrier", "outbox"])
def test_service_draining_barrier_injections_restore_staged_rows(monkeypatch, inject_at) -> None:
    _task, target, _siblings, aggregate = _aggregate_for_sibling_boundaries(
        (CoordinationRuntimeBoundary.CROSSED_ACTIVE,)
    )
    state, service = _service_state(monkeypatch, aggregate, target)
    state.inject_at = inject_at
    before = _durable_fingerprint(state)
    with pytest.raises(RuntimeError, match="injected"):
        _call(service, target, phase=RuntimePhase.FAILED, now=_now_for(target))
    assert _durable_fingerprint(state) == before
    assert state.lifecycle_count == 0
    assert state.outbox_count == 0
    assert state.commits == 0
    assert state.rollbacks == 1


@pytest.mark.parametrize(
    ("boundary", "expected_saves"),
    [
        (CoordinationRuntimeBoundary.NOT_CROSSED_QUEUED, 1),
        (CoordinationRuntimeBoundary.NOT_CROSSED_PREPARED, 1),
    ],
)
def test_service_task_save_is_not_duplicated_for_release_then_failed_completion(
    monkeypatch, boundary, expected_saves
) -> None:
    _task, target, _siblings, aggregate = _aggregate_for_sibling_boundaries((boundary,))
    state, service = _service_state(monkeypatch, aggregate, target)
    _call(service, target, phase=RuntimePhase.FAILED, now=_now_for(target))
    assert state.task_saves == expected_saves


def test_service_existing_reconciliation_hold_waits_without_duplicate_task_save(
    monkeypatch,
) -> None:
    task, target, _siblings, aggregate = _aggregate_for_sibling_boundaries(
        (CoordinationRuntimeBoundary.RECONCILIATION_EVIDENCE,),
        drain_target=CoordinationRuntimeDrainTarget.RUNNING,
    )
    aggregate = replace(
        aggregate,
        task=replace(task, status=TaskStatus.RECONCILIATION_REQUIRED),
    )
    state, service = _service_state(monkeypatch, aggregate, target)
    result, _observation_value = _call(
        service, target, phase=RuntimePhase.FAILED, now=_now_for(target)
    )
    assert result.kind is CoordinatedKnownTerminalKind.DRAINING_RECONCILIATION
    assert state.task_saves == 0
    assert state.commits == 1


def test_service_read_only_replay_close_is_not_counted_as_rollback(monkeypatch) -> None:
    _task, target, aggregate = _aggregate()
    state, service = _service_state(monkeypatch, aggregate, target)
    now = _now_for(target)
    _first, observation = _call(service, target, phase=RuntimePhase.SUCCEEDED, now=now)
    rollback_count = state.rollbacks
    replay, _ = _call(
        service,
        target,
        phase=RuntimePhase.SUCCEEDED,
        now=now + timedelta(seconds=2),
        observation=observation,
    )
    assert replay.kind is CoordinatedKnownTerminalKind.REPLAY
    assert state.rollbacks == rollback_count
    assert "uow.close" in state.operation_log


def test_convergence_ast_forbids_external_wiring_and_extra_transactions() -> None:
    root = Path(__file__).parents[1] / "src" / "agentmesh"
    convergence = root / "application" / "coordinated_runtime_convergence.py"
    module = ast.parse(convergence.read_text(encoding="utf-8"), filename=str(convergence))
    method = next(
        value
        for value in ast.walk(module)
        if isinstance(value, ast.FunctionDef) and value.name == "apply_known_terminal"
    )
    assert sum(
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == "_uow_factory"
        for value in ast.walk(method)
    ) == 1
    assert sum(
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == "commit"
        for value in ast.walk(method)
    ) == 1
    forbidden = (
        "agentmesh.adapters",
        "agentmesh.worker",
        "agentmesh.application.admission",
        "agentmesh.features",
        "agentmesh.config",
    )
    imported_modules = [
        node.module or ""
        for node in ast.walk(module)
        if isinstance(node, ast.ImportFrom)
    ] + [
        alias.name
        for node in ast.walk(module)
        if isinstance(node, ast.Import)
        for alias in node.names
    ]
    assert not any(
        any(module_name == prefix or module_name.startswith(prefix + ".") for prefix in forbidden)
        for module_name in imported_modules
    )
    # The service is intentionally a closed command.  Its module may be
    # imported only by tests (and this definition itself); production wiring
    # must wait for the later admission/worker slice.
    external_service_refs = []
    for path in root.rglob("*.py"):
        if path == convergence:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if any(
                    alias.name == "CoordinatedRuntimeConvergenceService"
                    for alias in node.names
                ):
                    external_service_refs.append(f"{path}:{node.lineno}")
            elif isinstance(node, ast.Import):
                if any(
                    alias.name == "agentmesh.application.coordinated_runtime_convergence"
                    for alias in node.names
                ):
                    external_service_refs.append(f"{path}:{node.lineno}")
            elif isinstance(node, ast.Name) and node.id == "CoordinatedRuntimeConvergenceService":
                external_service_refs.append(f"{path}:{node.lineno}")
            elif isinstance(node, ast.Call):
                function = node.func
                if (
                    isinstance(function, ast.Name)
                    and function.id == "CoordinatedRuntimeConvergenceService"
                ) or (
                    isinstance(function, ast.Attribute)
                    and function.attr == "CoordinatedRuntimeConvergenceService"
                ):
                    external_service_refs.append(f"{path}:{node.lineno}")
    assert external_service_refs == []
    barrier = root / "application" / "coordinated_runtime_barrier.py"
    barrier_tree = ast.parse(barrier.read_text(encoding="utf-8"), filename=str(barrier))
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"_uow_factory", "commit"}
        for node in ast.walk(barrier_tree)
    )
    external_calls = []
    for path in root.rglob("*.py"):
        if path == convergence:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "apply_known_terminal":
                    external_calls.append(f"{path}:{node.lineno}")
    assert external_calls == []


@pytest.mark.parametrize(
    "name",
    [
        "attempt_id",
        "run_id",
        "fence",
        "attempt_status",
        "execution_id",
        "execution_tenant",
        "execution_version",
        "execution_owner",
        "execution_fence",
        "execution_phase",
        "run_authority",
        "run_comparison",
        "run_version",
        "cohort",
        "task_status",
        "task_tenant",
        "task_mode",
        "boundary",
        "snapshot_duplicate",
        "snapshot_tenant",
        "snapshot_execution",
        "snapshot_assignment",
        "snapshot_digest",
        "snapshot_time",
    ],
)
def test_select_target_identity_and_authority_mismatch_matrix_is_fail_closed(name) -> None:
    _task, target, aggregate = _aggregate()
    run = target[1]
    attempt = target[2]
    execution = target[3]
    if name == "attempt_id":
        attempt_id = uuid4()
    else:
        attempt_id = attempt.id
    if name == "run_id":
        run_id = uuid4()
    else:
        run_id = run.id
    fencing_token = attempt.fencing_token
    runtime_execution_id = execution.id
    if name == "fence":
        fencing_token += 1
    broken = aggregate
    if name.startswith("snapshot_"):
        snapshot = SimpleNamespace(
            tenant_id="tenant-a",
            runtime_execution_id=execution.id,
            assignment_id=execution.assignment_id,
            assignment_digest=execution.assignment_digest,
            created_at=execution.updated_at,
            canonical_payload={},
        )
        if name == "snapshot_duplicate":
            snapshots = (snapshot, SimpleNamespace(**vars(snapshot)))
        else:
            snapshots = (snapshot,)
            if name == "snapshot_tenant":
                snapshots = (
                    SimpleNamespace(**{**vars(snapshot), "tenant_id": "tenant-other"}),
                )
            elif name == "snapshot_execution":
                snapshots = (
                    SimpleNamespace(
                        **{**vars(snapshot), "runtime_execution_id": uuid4()}
                    ),
                )
            elif name == "snapshot_assignment":
                snapshots = (SimpleNamespace(**{**vars(snapshot), "assignment_id": uuid4()}),)
            elif name == "snapshot_digest":
                snapshots = (SimpleNamespace(**{**vars(snapshot), "assignment_digest": "c" * 64}),)
            elif name == "snapshot_time":
                snapshots = (
                    SimpleNamespace(
                        **{
                            **vars(snapshot),
                            "created_at": execution.updated_at + timedelta(days=1),
                        }
                    ),
                )
        broken = replace(broken, assignment_snapshots=snapshots)
    if name == "attempt_status":
        broken = replace(
            broken,
            latest_attempts={run.id: replace(attempt, status=AttemptStatus.PAUSED)},
        )
    elif name.startswith("execution_"):
        value = execution
        if name == "execution_id":
            runtime_execution_id = uuid4()
        if name == "execution_tenant":
            value = replace(value, tenant_id="tenant-other")
        elif name == "execution_version":
            value = replace(value, runtime_version_id=uuid4())
        elif name == "execution_owner":
            value = replace(value, current_owner_attempt_id=uuid4())
        elif name == "execution_fence":
            value = replace(value, current_fencing_token=fencing_token + 1)
        elif name == "execution_phase":
            value = replace(value, phase=RuntimeExecutionPhase.SUCCEEDED)
        broken = replace(broken, executions=(value,))
    else:
        runtime_execution_id = execution.id
    if name == "run_authority":
        broken = replace(broken, runs=(replace(run, runtime_authority="legacy"),))
    elif name == "run_comparison":
        broken = replace(broken, runs=(replace(run, comparison_mode="strict"),))
    elif name == "run_version":
        broken = replace(broken, runs=(replace(run, runtime_version_id=uuid4()),))
    elif name == "cohort":
        broken = replace(broken, cohort=replace(broken.cohort, task_id=uuid4()))
    elif name == "task_status":
        broken = replace(broken, task=replace(broken.task, status=TaskStatus.CREATED))
    elif name == "task_tenant":
        broken = replace(broken, task=replace(broken.task, tenant_id="tenant-other"))
    elif name == "task_mode":
        broken = replace(broken, task=replace(broken.task, execution_mode=TaskExecutionMode.DIRECT))
    elif name == "boundary":
        broken = replace(
            broken,
            boundary_classifications={
                run.id: CoordinationRuntimeBoundary.KNOWN_TERMINAL
            },
        )
    with pytest.raises((RuntimeExecutionConflict, InvalidTaskTransition)):
        _select_target(
            broken,
            tenant_id="tenant-a",
            task_id=run.task_id,
            run_id=run_id,
            attempt_id=attempt_id,
            fencing_token=fencing_token,
            runtime_execution_id=runtime_execution_id,
            received_at=_now_for(target) + timedelta(seconds=1),
        )


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
