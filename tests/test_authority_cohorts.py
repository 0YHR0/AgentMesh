from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agentmesh.application.authority_cohorts import (
    AuthorityCohort,
    AuthorityCohortResolver,
    ContinuationKind,
)
from agentmesh.domain.errors import (
    InvalidTaskInput,
    InvalidTaskTransition,
    RuntimeExecutionConflict,
    RuntimeVersionNotFound,
)
from agentmesh.domain.runtime_execution import RuntimeTrustProfile, RuntimeVersionStatus
from agentmesh.domain.tasks import (
    AcceptanceCriterion,
    AcceptanceCriterionKind,
    RunRole,
    Task,
    TaskExecutionMode,
    TaskRun,
)
from agentmesh.features import FeatureGateSet
from agentmesh.runtime_sdk.builtin import (
    LANGGRAPH_V2_DESCRIPTOR,
    builtin_langgraph_runtime_id,
    builtin_langgraph_version_id,
)
from agentmesh.runtime_sdk.canonical import canonical_digest


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


class _Tasks:
    def __init__(self, task=None):
        self.task = task

    def get(self, task_id, *, for_update=False):
        return self.task if self.task is not None and self.task.id == task_id else None


class _Uow:
    def __init__(self, runs=(), version=None, task=None):
        self.runs = _Runs(runs)
        self.runtimes = _RuntimeRepo(version)
        self.tasks = _Tasks(task) if task is not None else None


def _task():
    return Task.create(tenant_id="tenant-a", objective="objective")


def _version(status=RuntimeVersionStatus.PUBLISHED):
    return SimpleNamespace(
        id=builtin_langgraph_version_id("v2"),
        runtime_id=builtin_langgraph_runtime_id(),
        status=status,
        api_version=1,
        adapter_kind="python-in-process",
        descriptor=LANGGRAPH_V2_DESCRIPTOR,
        configuration_digest=canonical_digest(
            {
                "runtime_key": LANGGRAPH_V2_DESCRIPTOR["runtime_key"],
                "capabilities": LANGGRAPH_V2_DESCRIPTOR["capabilities"],
                "limits": LANGGRAPH_V2_DESCRIPTOR["limits"],
            }
        ),
        artifact_digest=canonical_digest(
            {"package": "agentmesh", "runtime": "agentmesh.langgraph", "release": "v2"}
        ),
        trust_profile=RuntimeTrustProfile.BUILT_IN,
        compatibility={},
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


def test_initial_admission_requires_zero_prior_runs_and_does_not_reread_gate():
    task = _task()
    first = TaskRun.request(task.id, "agent")
    resolver = AuthorityCohortResolver(feature_gates=FeatureGateSet.from_config("minimal"))
    with pytest.raises(InvalidTaskTransition):
        resolver.initial_admission_in_uow(_Uow([first], task=task), task)


def test_non_direct_initial_admission_rejects_explicit_runtime_version():
    task = Task.create(
        tenant_id="tenant-a",
        objective="objective",
        execution_mode=TaskExecutionMode.REVIEWED,
        acceptance_criteria=(
            AcceptanceCriterion.create(
                key="output",
                description="output",
                kind=AcceptanceCriterionKind.OUTPUT_PATH_EXISTS,
                path=["output"],
            ),
        ),
    )
    resolver = AuthorityCohortResolver(feature_gates=FeatureGateSet.from_config("minimal"))
    with pytest.raises(InvalidTaskInput):
        resolver.initial_admission_in_uow(_Uow(task=task), task, runtime_version_id=uuid4())


def test_authority_cohort_invariants_are_closed():
    from agentmesh.application.authority_cohorts import AuthorityCohort

    with pytest.raises(InvalidTaskInput):
        AuthorityCohort("legacy", uuid4(), "off")
    with pytest.raises(InvalidTaskInput):
        AuthorityCohort("legacy", None, "deterministic_shadow")
    with pytest.raises(InvalidTaskInput):
        AuthorityCohort("managed", uuid4(), "deterministic_shadow")


def test_forged_parent_and_lineage_kinds_fail_closed():
    task = _task()
    parent = TaskRun.request(task.id, "agent")
    forged = TaskRun.request(task.id, "agent")
    forged.id = parent.id
    resolver = AuthorityCohortResolver(feature_gates=FeatureGateSet.from_config("minimal"))
    with pytest.raises(InvalidTaskTransition):
        resolver.create_continuation_in_uow(
            _Uow([parent], task=task),
            task,
            agent_id="agent",
            agent_version_id=None,
            agent_version_digest=None,
            role=RunRole.REVIEWER,
            parent_run=forged,
        )


@pytest.mark.parametrize("mutation", ["identity", "descriptor", "configuration"])
def test_inherited_runtime_requires_exact_builtin_contract(mutation):
    task = _task()
    version = _version(RuntimeVersionStatus.DEPRECATED)
    parent = TaskRun.request(
        task.id, "agent", runtime_authority="managed", runtime_version_id=version.id
    )
    if mutation == "identity":
        version.id = uuid4()
    elif mutation == "descriptor":
        version.descriptor = {**LANGGRAPH_V2_DESCRIPTOR, "runtime_key": "evil.runtime"}
    else:
        version.configuration_digest = "0" * 64
    resolver = AuthorityCohortResolver(feature_gates=FeatureGateSet.from_config("minimal"))
    with pytest.raises(RuntimeVersionNotFound):
        resolver.create_continuation_in_uow(
            _Uow([parent], version),
            task,
            agent_id="agent",
            agent_version_id=None,
            agent_version_digest=None,
            role=RunRole.REVIEWER,
            parent_run=parent,
        )


def test_resolved_cohort_cannot_be_reused_by_another_task():
    task = _task()
    other = _task()
    resolver = AuthorityCohortResolver(feature_gates=FeatureGateSet.from_config("minimal"))
    cohort = resolver.resolve_continuation_cohort_in_uow(_Uow(task=task), task)
    with pytest.raises(RuntimeExecutionConflict):
        resolver.create_continuation_from_cohort_in_uow(
            _Uow(task=other),
            other,
            cohort=cohort,
            agent_id="agent",
            agent_version_id=None,
            agent_version_digest=None,
            role=RunRole.EXECUTOR,
            subtask_id=uuid4(),
            kind=ContinuationKind.COORDINATED,
        )


@pytest.mark.parametrize(
    "task_mode, role, subtask_id",
    [
        (TaskExecutionMode.DIRECT, RunRole.EXECUTOR, uuid4()),
        (TaskExecutionMode.COORDINATED, RunRole.EXECUTOR, None),
        (TaskExecutionMode.COORDINATED, RunRole.SUPERVISOR, uuid4()),
    ],
)
def test_coordinated_lineage_requires_mode_and_role_binding(task_mode, role, subtask_id):
    task = Task.create(
        tenant_id="tenant-a",
        objective="objective",
        execution_mode=task_mode,
        plan_version=1 if task_mode is TaskExecutionMode.COORDINATED else None,
        plan_digest="sha256:plan" if task_mode is TaskExecutionMode.COORDINATED else None,
        max_concurrency=1,
    )
    cohort = AuthorityCohort("legacy", None, "off", task_id=task.id, tenant_id=task.tenant_id)
    resolver = AuthorityCohortResolver(feature_gates=FeatureGateSet.from_config("minimal"))
    with pytest.raises(InvalidTaskTransition):
        resolver.create_continuation_from_cohort_in_uow(
            _Uow(task=task),
            task,
            cohort=cohort,
            agent_id="agent",
            agent_version_id=None,
            agent_version_digest=None,
            role=role,
            subtask_id=subtask_id,
            kind=ContinuationKind.COORDINATED,
        )


def test_local_continuation_services_have_no_raw_run_request_bypass():
    root = Path(__file__).parents[1] / "src" / "agentmesh" / "application"
    for name in ("services.py", "coordination_services.py", "resolution_services.py"):
        source = (root / name).read_text(encoding="utf-8")
        assert "TaskRun.request(" not in source
