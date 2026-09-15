from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agentmesh.application.resolution_services import TaskResolutionService
from agentmesh.domain.coordination import (
    CoordinationRuntimeBoundary,
    CoordinationRuntimeDrain,
    CoordinationRuntimeDrainStatus,
    CoordinationRuntimeDrainTarget,
)
from agentmesh.domain.errors import IdempotencyConflict, InvalidTaskTransition
from agentmesh.domain.tasks import RunRole, TaskExecutionMode, TaskStatus
from agentmesh.features import FeatureGateSet
from tests.fakes import InMemoryIdempotencyRepository
from tests.test_coordinated_runtime_barrier import _aggregate_for_sibling_boundaries


class _Repo:
    def __init__(self, uow, name: str) -> None:
        self.uow = uow
        self.name = name

    def get_task(self, task_id, *, for_update=False):
        self.uow.operations.append(("task.get", for_update))
        task = self.uow.aggregate.task
        return task if task.id == task_id else None

    def save_task(self, task):
        self.uow.operations.append(("task.save", task.id))

    def list_runs(self, task_id, **_kwargs):
        return list(self.uow.aggregate.runs)

    def list_attempts(self, task_id):
        return [
            value
            for value in self.uow.aggregate.latest_attempts.values()
            if value is not None
        ]

    def list_subtasks(self, task_id, **_kwargs):
        return list(self.uow.aggregate.subtasks)

    @staticmethod
    def empty(_task_id):
        return []


class _Uow:
    def __init__(self, factory) -> None:
        self.factory = factory
        self.aggregate = replace(
            factory.aggregate,
            task=deepcopy(factory.aggregate.task),
            active_drain=deepcopy(factory.aggregate.active_drain),
        )
        self.drain = deepcopy(factory.drain)
        self.idem_values = deepcopy(factory.idem_values)
        self.resolutions = deepcopy(factory.resolutions)
        self.events = deepcopy(factory.events)
        self.operations = factory.operations
        repo = _Repo(self, "aggregate")
        self.tasks = SimpleNamespace(get=repo.get_task, save=repo.save_task)
        self.runs = SimpleNamespace(list_for_task=repo.list_runs)
        self.attempts = SimpleNamespace(list_for_task=repo.list_attempts)
        self.subtasks = SimpleNamespace(list_for_task=repo.list_subtasks)
        self.subtask_dependencies = SimpleNamespace(list_for_task=repo.empty)
        self.handoffs = SimpleNamespace(list_for_task=repo.empty)
        self.idempotency = InMemoryIdempotencyRepository(self.idem_values)
        original_lock = self.idempotency.lock
        original_get = self.idempotency.get
        original_add = self.idempotency.add

        def idem_lock(scope, key):
            self.operations.append(("idem.lock", key))
            return original_lock(scope, key)

        def idem_get(scope, key):
            self.operations.append(("idem.get", key))
            return original_get(scope, key)

        def idem_add(value):
            self.operations.append(("idem.add", value.key))
            if self.factory.fail_on == "idem.add":
                raise RuntimeError("injected idempotency failure")
            return original_add(value)

        self.idempotency.lock = idem_lock
        self.idempotency.get = idem_get
        self.idempotency.add = idem_add
        self.coordination_runtime_drains = SimpleNamespace(
            save=self._save_drain,
            get=self._get_drain,
        )
        self.task_resolutions = SimpleNamespace(
            add=self._add_resolution,
            get=lambda identity: self.resolutions.get(identity),
            list_for_task=lambda task_id: [
                value for value in self.resolutions.values() if value.task_id == task_id
            ],
        )
        self.outbox = SimpleNamespace(add=self._add_event, get=self._get_event)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def _save_drain(self, value, *, tenant_id):
        self.operations.append(("drain.save", value.id))
        assert value.tenant_id == tenant_id
        self.drain = deepcopy(value)

    def _get_drain(self, drain_id, *, tenant_id, for_update=False):
        self.operations.append(("drain.get", for_update))
        if self.drain.id == drain_id and self.drain.tenant_id == tenant_id:
            return deepcopy(self.drain)
        return None

    def _add_resolution(self, value):
        self.operations.append(("resolution.add", value.id))
        if self.factory.fail_on == "resolution.add":
            raise RuntimeError("injected resolution failure")
        self.resolutions[value.id] = deepcopy(value)

    def _add_event(self, value):
        self.operations.append(("outbox.add", value.message_id))
        if self.factory.fail_on == "outbox.add":
            raise RuntimeError("injected outbox failure")
        self.events[value.message_id] = deepcopy(value)

    def _get_event(self, message_id, *, tenant_id):
        self.operations.append(("outbox.get", message_id))
        value = self.events.get(message_id)
        return deepcopy(value) if value is not None and value.tenant_id == tenant_id else None

    def commit(self):
        self.operations.append(("commit", None))
        if self.factory.fail_on == "commit":
            raise RuntimeError("injected commit failure")
        self.factory.commits += 1
        self.factory.aggregate = replace(
            self.aggregate,
            task=deepcopy(self.aggregate.task),
            active_drain=(
                None
                if self.drain.status is CoordinationRuntimeDrainStatus.COMPLETE
                else deepcopy(self.drain)
            ),
        )
        self.factory.drain = deepcopy(self.drain)
        self.factory.idem_values = deepcopy(self.idem_values)
        self.factory.resolutions = deepcopy(self.resolutions)
        self.factory.events = deepcopy(self.events)


class _Factory:
    def __init__(self, aggregate, drain, *, fail_on=None) -> None:
        self.aggregate = aggregate
        self.drain = drain
        self.fail_on = fail_on
        self.idem_values = {}
        self.resolutions = {}
        self.events = {}
        self.operations = []
        self.commits = 0

    def __call__(self):
        return _Uow(self)


class _Locker:
    def lock_after_task(self, uow, task, *, tenant_id, task_id):
        uow.operations.append(("aggregate.lock_after_task", None))
        assert task is uow.aggregate.task
        assert task.tenant_id == tenant_id and task.id == task_id
        return uow.aggregate


def _fixture(*, fail_on=None, unfinished=False):
    _task, _target, siblings, aggregate = _aggregate_for_sibling_boundaries(
        (CoordinationRuntimeBoundary.KNOWN_TERMINAL,)
    )
    _subtask, run, attempt, execution = siblings[0]
    aggregate = replace(
        aggregate,
        subtasks=(_subtask,),
        runs=(run,),
        latest_attempts={run.id: attempt},
        executions=(execution,),
        assignment_snapshots=tuple(
            value
            for value in aggregate.assignment_snapshots
            if value.runtime_execution_id == execution.id
        ),
        handle_snapshots=tuple(
            value
            for value in aggregate.handle_snapshots
            if value.runtime_execution_id == execution.id
        ),
        lifecycle_operations=tuple(
            value
            for value in aggregate.lifecycle_operations
            if value.runtime_execution_id == execution.id
        ),
        integrity_incidents=tuple(
            value
            for value in aggregate.integrity_incidents
            if value.runtime_execution_id == execution.id
        ),
        boundary_classifications={run.id: CoordinationRuntimeBoundary.KNOWN_TERMINAL},
    )
    now = aggregate.task.created_at
    candidate = {"report": "approved"}
    supervisor = replace(run, role=RunRole.SUPERVISOR, subtask_id=None, output=candidate)
    task = replace(
        aggregate.task,
        execution_mode=TaskExecutionMode.COORDINATED,
        status=TaskStatus.WAITING_APPROVAL,
        current_run_id=None,
        output=None,
        candidate_output=candidate,
        error="budget.max_cost",
        budget_exhausted_reason="budget.max_cost",
    )
    drain = CoordinationRuntimeDrain.start(
        drain_id=uuid4(),
        tenant_id=task.tenant_id,
        task_id=task.id,
        triggering_run_id=supervisor.id,
        target=CoordinationRuntimeDrainTarget.WAITING_APPROVAL,
        reason=task.error,
        at=now,
    )
    aggregate = replace(
        aggregate,
        task=task,
        active_drain=drain,
        subtasks=(),
        runs=(supervisor,),
        latest_attempts={supervisor.id: next(iter(aggregate.latest_attempts.values()))},
        boundary_classifications={
            supervisor.id: (
                CoordinationRuntimeBoundary.CROSSED_ACTIVE
                if unfinished
                else CoordinationRuntimeBoundary.KNOWN_TERMINAL
            )
        },
    )
    factory = _Factory(aggregate, drain, fail_on=fail_on)
    service = TaskResolutionService(
        uow_factory=factory,
        tenant_id=task.tenant_id,
        executor_agent_id="executor",
        reviewer_agent_id="reviewer",
        supervisor_agent_id="supervisor",
        feature_gates=FeatureGateSet.from_config("full"),
        coordinated_aggregate_locker=_Locker(),
    )
    service._scheduler.schedule = lambda *_args, **_kwargs: pytest.fail(
        "scheduler must not run"
    )
    return SimpleNamespace(factory=factory, service=service, task=task)


def _request(fixture, **changes):
    return {
        "task_id": fixture.task.id,
        "actor": "operator",
        "reason": "Approve the Supervisor candidate",
        "idempotency_key": "accept-supervisor-1",
        **changes,
    }


def test_coordinated_supervisor_candidate_accepts_atomically_and_replays_read_only():
    fixture = _fixture()

    first = fixture.service.accept_candidate(**_request(fixture))
    assert first.aggregate.task.status is TaskStatus.COMPLETED
    assert first.aggregate.task.output == {"report": "approved"}
    assert fixture.factory.drain.status is CoordinationRuntimeDrainStatus.COMPLETE
    assert fixture.factory.commits == 1
    assert len(fixture.factory.resolutions) == len(fixture.factory.events) == 1
    task_get = fixture.factory.operations.index(("task.get", True))
    aggregate_lock = fixture.factory.operations.index(("aggregate.lock_after_task", None))
    idem_lock = next(
        index
        for index, value in enumerate(fixture.factory.operations)
        if value[0] == "idem.lock"
    )
    assert task_get < aggregate_lock < idem_lock

    fixture.factory.operations.clear()
    replay = fixture.service.accept_candidate(**_request(fixture))
    assert replay.resolution.id == first.resolution.id
    assert fixture.factory.commits == 1
    assert all(
        value[0] not in {"task.save", "drain.save", "outbox.add"}
        for value in fixture.factory.operations
    )
    assert not any(value[0] == "commit" for value in fixture.factory.operations)


def test_coordinated_candidate_same_key_different_request_conflicts():
    fixture = _fixture()
    fixture.service.accept_candidate(**_request(fixture))

    with pytest.raises(IdempotencyConflict):
        fixture.service.accept_candidate(**_request(fixture, reason="changed"))

    assert fixture.factory.commits == 1


def test_coordinated_candidate_rejects_unfinished_sibling_before_mutation():
    fixture = _fixture(unfinished=True)

    with pytest.raises(InvalidTaskTransition, match="unfinished siblings"):
        fixture.service.accept_candidate(**_request(fixture))

    assert fixture.factory.commits == 0
    assert not fixture.factory.resolutions
    assert not fixture.factory.events


def test_coordinated_candidate_rejects_missing_boundary_classification():
    fixture = _fixture()
    fixture.factory.aggregate = replace(
        fixture.factory.aggregate,
        boundary_classifications={},
    )

    with pytest.raises(InvalidTaskTransition, match="unfinished siblings"):
        fixture.service.accept_candidate(**_request(fixture))

    assert fixture.factory.commits == 0
    assert not fixture.factory.resolutions


@pytest.mark.parametrize(
    "fail_on",
    ["resolution.add", "outbox.add", "idem.add", "commit"],
)
def test_coordinated_candidate_failure_rolls_back_every_projection(fail_on):
    fixture = _fixture(fail_on=fail_on)

    with pytest.raises(RuntimeError, match="injected"):
        fixture.service.accept_candidate(**_request(fixture))

    assert fixture.factory.commits == 0
    assert fixture.factory.aggregate.task.status is TaskStatus.WAITING_APPROVAL
    assert fixture.factory.drain.status is CoordinationRuntimeDrainStatus.DRAINING
    assert not fixture.factory.resolutions
    assert not fixture.factory.events
    assert not fixture.factory.idem_values


@pytest.mark.parametrize("corruption", ["drain", "resolution", "outbox", "idempotency"])
def test_coordinated_candidate_replay_rejects_partial_projection(corruption):
    fixture = _fixture()
    fixture.service.accept_candidate(**_request(fixture))
    if corruption == "drain":
        fixture.factory.drain = replace(
            fixture.factory.drain,
            target=CoordinationRuntimeDrainTarget.CANCELED,
        )
    elif corruption == "resolution":
        identity = next(iter(fixture.factory.resolutions))
        value = fixture.factory.resolutions[identity]
        fixture.factory.resolutions[identity] = replace(value, details={})
    elif corruption == "outbox":
        identity = next(iter(fixture.factory.events))
        value = fixture.factory.events[identity]
        fixture.factory.events[identity] = replace(value, payload={"corrupt": True})
    else:
        identity = next(iter(fixture.factory.idem_values))
        value = fixture.factory.idem_values[identity]
        fixture.factory.idem_values[identity] = replace(
            value,
            result={
                key: item
                for key, item in value.result.items()
                if key != "outbox_event_id"
            },
        )
    fixture.factory.operations.clear()

    with pytest.raises(InvalidTaskTransition):
        fixture.service.accept_candidate(**_request(fixture))

    assert fixture.factory.commits == 1
    assert not any(value[0] == "commit" for value in fixture.factory.operations)


def test_coordinated_candidate_replay_binds_original_actor_and_reason():
    fixture = _fixture()
    fixture.service.accept_candidate(**_request(fixture))
    resolution_id = next(iter(fixture.factory.resolutions))
    resolution = fixture.factory.resolutions[resolution_id]
    fixture.factory.resolutions[resolution_id] = replace(
        resolution,
        actor="tampered-operator",
        reason="tampered reason",
    )
    event_id = next(iter(fixture.factory.events))
    event = fixture.factory.events[event_id]
    fixture.factory.events[event_id] = replace(
        event,
        payload={**event.payload, "actor": "tampered-operator"},
    )

    with pytest.raises(InvalidTaskTransition, match="audit is inconsistent"):
        fixture.service.accept_candidate(**_request(fixture))

    assert fixture.factory.commits == 1


def test_coordinated_candidate_replay_rejects_non_mapping_idempotency_result():
    fixture = _fixture()
    fixture.service.accept_candidate(**_request(fixture))
    identity = next(iter(fixture.factory.idem_values))
    value = fixture.factory.idem_values[identity]
    fixture.factory.idem_values[identity] = replace(value, result=[])

    with pytest.raises(InvalidTaskTransition, match="projection is invalid"):
        fixture.service.accept_candidate(**_request(fixture))

    assert fixture.factory.commits == 1
