from types import SimpleNamespace
from uuid import uuid4

import pytest

from agentmesh.application.authority_cohorts import AuthorityCohortResolver
from agentmesh.domain.errors import (
    InvalidTaskInput,
    RuntimeExecutionConflict,
    RuntimeVersionNotFound,
)
from agentmesh.domain.runtime_execution import RuntimeVersionStatus
from agentmesh.domain.tasks import RunRole, Task, TaskExecutionMode, TaskRun
from agentmesh.features import FeatureGateSet


class _Runs:
    def __init__(self, values=()):
        self.values = list(values)

    def list_for_task(self, task_id):
        return [value for value in self.values if value.task_id == task_id]

    def get(self, run_id, *, for_update=False):
        return next((value for value in self.values if value.id == run_id), None)


class _RuntimeRepo:
    def __init__(self, version=None):
        self.version = version

    def get_version(self, version_id, *, tenant_id, for_update=False):
        return self.version if self.version and self.version.id == version_id else None


class _Uow:
    def __init__(self, runs=(), version=None):
        self.runs = _Runs(runs)
        self.runtimes = _RuntimeRepo(version)


def _task():
    return Task.create(tenant_id="tenant-a", objective="objective")


def _version(status=RuntimeVersionStatus.PUBLISHED):
    return SimpleNamespace(
        id=uuid4(),
        status=status,
        api_version=1,
        descriptor={"runtime_key": "agentmesh.langgraph"},
    )


def test_initial_direct_uses_managed_only_when_gate_is_enabled():
    task = _task()
    version = _version()
    registry = SimpleNamespace(
        require_builtin_langgraph_v2_in_uow=lambda uow: version,
    )
    resolver = AuthorityCohortResolver(
        feature_gates=FeatureGateSet.from_config(
            "full",
            "managed_agent_runtime=true,managed_runtime_worker=true,"
            "managed_runtime_direct_cutover=true",
        ),
        runtime_registry_service=registry,
    )
    cohort = resolver.initial_admission_in_uow(_Uow(), task)
    assert cohort.runtime_authority == "managed"
    assert cohort.runtime_version_id == version.id


def test_managed_continuation_inherits_deprecated_version_and_new_intent():
    task = _task()
    version = _version(RuntimeVersionStatus.DEPRECATED)
    parent = TaskRun.request(
        task.id,
        "agent",
        runtime_authority="managed",
        runtime_version_id=version.id,
    )
    resolver = AuthorityCohortResolver(feature_gates=FeatureGateSet.from_config("minimal"))
    child = resolver.create_continuation_in_uow(
        _Uow([parent], version),
        task,
        agent_id="agent",
        agent_version_id=uuid4(),
        agent_version_digest="a" * 64,
        role=RunRole.REVIEWER,
        parent_run=parent,
    )
    assert child.runtime_authority == "managed"
    assert child.runtime_version_id == version.id
    assert child.runtime_execution_intent_id not in {None, parent.runtime_execution_intent_id}


def test_mixed_authority_and_revoked_version_fail_closed():
    task = _task()
    version = _version(RuntimeVersionStatus.REVOKED)
    managed = TaskRun.request(
        task.id, "agent", runtime_authority="managed", runtime_version_id=version.id
    )
    legacy = TaskRun.request(task.id, "agent")
    resolver = AuthorityCohortResolver(feature_gates=FeatureGateSet.from_config("minimal"))
    with pytest.raises(RuntimeExecutionConflict):
        resolver.create_continuation_in_uow(
            _Uow([managed, legacy], version),
            task,
            agent_id="agent",
            agent_version_id=None,
            agent_version_digest=None,
            role=RunRole.EXECUTOR,
        )
    with pytest.raises(RuntimeVersionNotFound):
        resolver.create_continuation_in_uow(
            _Uow([managed], version),
            task,
            agent_id="agent",
            agent_version_id=None,
            agent_version_digest=None,
            role=RunRole.REVIEWER,
            parent_run=managed,
        )


def test_reviewed_or_coordinated_initial_shadow_is_rejected():
    task = Task.create(
        tenant_id="tenant-a",
        objective="objective",
        execution_mode=TaskExecutionMode.COORDINATED,
        plan_version=1,
        plan_digest="sha256:plan",
    )
    resolver = AuthorityCohortResolver(feature_gates=FeatureGateSet.from_config("minimal"))
    with pytest.raises(InvalidTaskInput):
        resolver.initial_admission_in_uow(
            _Uow(), task, runtime_version_id=uuid4(), comparison_mode="deterministic_shadow"
        )
