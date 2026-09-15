"""A4.2d legacy/managed REVIEWED parity qualification fixtures.

These tests intentionally drive the two worker finalizer entry points rather
than invoking :class:`BusinessOutcomeApplier` directly.  The scenario inputs
are held constant; only the authority and the terminal-evidence adapter differ.
Runtime identifiers, message identifiers, and control-plane timestamps are
normalized through explicit, narrow rules in ``_projection``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

import pytest

from agentmesh.application.quota_services import QuotaPolicyService
from agentmesh.application.registry_services import AgentRegistryService
from agentmesh.application.services import RunExecutionService, TaskApplicationService
from agentmesh.domain.budgets import TaskBudget
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.domain.observability import UsageRecord
from agentmesh.domain.quotas import QuotaScope
from agentmesh.domain.tasks import (
    AcceptanceCriterion,
    AcceptanceCriterionKind,
    RunRole,
    TaskExecutionMode,
    TaskRun,
    utc_now,
)
from agentmesh.features import FeatureGateSet
from agentmesh.runtime_sdk import RuntimePhase
from tests.fakes import InMemoryUnitOfWorkFactory
from tests.test_task_service import (
    _CapturingWorkflowRunner,
    _managed_reviewed_finalizer_case,
    _MemoryCaptureProbe,
)

TENANT = "parity-tenant"
CANDIDATE = {"summary": "candidate"}
REVIEW_ACCEPT = {
    "criteria": [{"key": "summary", "passed": True}],
    "feedback": [],
}
REVIEW_REJECT = {
    "criteria": [{"key": "summary", "passed": False}],
    "feedback": ["needs another pass"],
}


@dataclass(frozen=True)
class _Scenario:
    name: str
    role: RunRole
    phase: RuntimePhase
    review_output: dict[str, Any] | None = None
    max_revisions: int = 1
    deadline_expired: bool = False
    budget_wait: bool = False


# This is deliberately machine-readable and is consumed by the test below.
# It is also the review checklist for the A4.2d unit qualification slice.
PARITY_FIXTURE_REPORT: tuple[dict[str, str], ...] = (
    {"scenario": "accept", "role": "REVIEWER", "terminal": "SUCCEEDED"},
    {"scenario": "revise", "role": "REVIEWER", "terminal": "SUCCEEDED"},
    {"scenario": "revision_limit", "role": "REVIEWER", "terminal": "SUCCEEDED"},
    {"scenario": "review_deadline", "role": "REVIEWER", "terminal": "SUCCEEDED"},
    {"scenario": "budget_wait", "role": "EXECUTOR", "terminal": "SUCCEEDED"},
    {"scenario": "failure", "role": "EXECUTOR", "terminal": "FAILED"},
    {"scenario": "timeout", "role": "EXECUTOR", "terminal": "TIMED_OUT"},
)


def _gates(*, managed: bool) -> FeatureGateSet:
    if managed:
        return FeatureGateSet.from_config(
            "full",
            (
                "managed_agent_runtime=true,managed_runtime_worker=true,"
                "managed_runtime_reviewed_cutover=true,"
                "identity_rbac=true,quota_admission=true"
            ),
        )
    return FeatureGateSet.from_config(
        "full",
        (
            "managed_agent_runtime=false,managed_runtime_worker=false,"
            "managed_runtime_reviewed_cutover=false,"
            "identity_rbac=true,quota_admission=true"
        ),
    )


def _criterion() -> AcceptanceCriterion:
    return AcceptanceCriterion.create(
        key="summary",
        description="Summary exists",
        kind=AcceptanceCriterionKind.OUTPUT_PATH_EXISTS,
        path=("summary",),
    )


def _budget_for(scenario: _Scenario) -> TaskBudget | None:
    if not scenario.budget_wait:
        return None
    # The first admitted Run is allowed; the terminalizer then sees max_runs
    # exhausted and must retain the candidate in WAITING_APPROVAL.
    return TaskBudget.create(
        max_runs=1,
        max_tokens=100,
        token_reservation_per_attempt=1,
    )


def _add_quota_policy(factory: InMemoryUnitOfWorkFactory) -> None:
    quotas = QuotaPolicyService(factory, TENANT)
    quotas.put_policy(
        scope=QuotaScope.TENANT,
        project_id=None,
        max_concurrent_attempts=2,
        weight=1,
        created_by="reviewed-parity",
    )
    quotas.put_policy(
        scope=QuotaScope.PROJECT,
        project_id="default",
        max_concurrent_attempts=2,
        weight=1,
        created_by="reviewed-parity",
    )


def _legacy_case(scenario: _Scenario):
    """Acquire a legacy Attempt and finalize through the legacy worker path."""
    factory = InMemoryUnitOfWorkFactory()
    agents = AgentRegistryService(uow_factory=factory, tenant_id=TENANT)
    agents.ensure_builtin_agent("test-agent")
    agents.ensure_builtin_agent("test-reviewer", reviewer=True)
    _add_quota_policy(factory)
    gates = _gates(managed=False)
    tasks = TaskApplicationService(
        uow_factory=factory,
        agent_id="test-agent",
        reviewer_agent_id="test-reviewer",
        tenant_id=TENANT,
        feature_gates=gates,
    )
    deadline = utc_now() + timedelta(hours=1)
    task_id = tasks.create_task(
        "legacy/managed parity",
        execution_mode=TaskExecutionMode.REVIEWED,
        acceptance_criteria=(_criterion(),),
        max_revisions=scenario.max_revisions,
        review_deadline=deadline,
        budget=_budget_for(scenario),
    ).task.id
    initial = tasks.request_run(task_id).runs[0]
    envelope = factory.store.outbox[-1]
    run = initial
    if scenario.role is RunRole.REVIEWER:
        reviewer_agent = agents.ensure_builtin_agent("test-reviewer", reviewer=True)
        reviewer_version = reviewer_agent.versions[-1]
        with factory() as uow:
            task = uow.tasks.get(task_id, for_update=True)
            executor = uow.runs.get(initial.id, for_update=True)
            assert task is not None and executor is not None
            at = utc_now()
            task.start(executor.id, at=at)
            executor.start(at=at)
            executor.succeed(CANDIDATE, at=at)
            reviewer = TaskRun.request(
                task.id,
                "test-reviewer",
                agent_version_id=reviewer_version.id,
                agent_version_digest=reviewer_version.content_digest,
                role=RunRole.REVIEWER,
                runtime_authority="legacy",
                at=at,
            )
            task.queue_review(executor.id, CANDIDATE, reviewer.id, at=at)
            reviewer_envelope = MessageEnvelope.run_requested(
                tenant_id=TENANT,
                task_id=task.id,
                run_id=reviewer.id,
                causation_id=envelope.message_id,
                at=at,
            )
            uow.runs.save(executor)
            uow.runs.add(reviewer)
            uow.tasks.save(task)
            uow.outbox.add(reviewer_envelope)
            uow.commit()
        envelope = reviewer_envelope
        run = reviewer
    runner = _CapturingWorkflowRunner(
        scenario.review_output if scenario.role is RunRole.REVIEWER else CANDIDATE
    )
    memory = _MemoryCaptureProbe()
    worker = RunExecutionService(
        uow_factory=factory,
        workflow_runner=runner,
        worker_id="legacy-parity-worker",
        consumer_name="legacy-parity-v1",
        lease_duration=timedelta(minutes=5),
        executor_agent_id="test-agent",
        reviewer_agent_id="test-reviewer",
        feature_gates=gates,
        runtime_memory_service=memory,
    )
    task, leased_run, attempt = worker._acquire(
        envelope, task_id=task_id, run_id=run.id
    )
    if scenario.deadline_expired:
        with factory() as uow:
            task = uow.tasks.get(task_id, for_update=True)
            assert task is not None
            task.review_deadline = utc_now() - timedelta(seconds=1)
            uow.tasks.save(task)
            uow.commit()
    if scenario.phase is RuntimePhase.SUCCEEDED:
        usage = ()
        if scenario.budget_wait:
            usage = (
                UsageRecord.create(
                    tenant_id=TENANT,
                    task_id=task_id,
                    run_id=leased_run.id,
                    attempt_id=attempt.id,
                    trace_id=attempt.trace_id,
                    provider="parity",
                    model="deterministic",
                    usage_details={"total": 0},
                    recorded_at=utc_now(),
                ),
            )
        worker._finalize_success(
            envelope,
            task_id,
            leased_run.id,
            attempt.id,
            scenario.review_output if scenario.role is RunRole.REVIEWER else CANDIDATE,
            usage_records=usage,
        )
    else:
        worker._finalize_failure(
            envelope,
            task_id,
            leased_run.id,
            attempt.id,
            f"runtime.{scenario.phase.value.lower()}",
        )
    return factory, tasks, memory, None


def _managed_case(scenario: _Scenario):
    """Acquire and finalize through the managed Runtime finalizer path."""
    runtime_phase = scenario.phase
    budget = _budget_for(scenario)
    case = _managed_reviewed_finalizer_case(
        role=scenario.role,
        phase=runtime_phase,
        budget=budget,
        quota=True,
        max_revisions=scenario.max_revisions,
        review_deadline=utc_now() + timedelta(hours=1),
    )
    factory, tasks, worker, envelope, result, registry, memory, task_id, attempt = case
    if scenario.deadline_expired:
        with factory() as uow:
            task = uow.tasks.get(task_id, for_update=True)
            assert task is not None
            task.review_deadline = utc_now() - timedelta(seconds=1)
            uow.tasks.save(task)
            uow.commit()
    if scenario.phase is RuntimePhase.SUCCEEDED:
        result = replace(
            result,
            observation=replace(
                result.observation,
                output=(
                    scenario.review_output
                    if scenario.role is RunRole.REVIEWER
                    else CANDIDATE
                ),
            ),
        )
    worker._finalize_managed(
        envelope,
        task_id=task_id,
        run_id=attempt.run_id,
        attempt_id=attempt.id,
        result=result,
    )
    return factory, tasks, memory, registry


def _run_projection(factory, tasks, memory, registry) -> dict[str, Any]:
    aggregate = tasks.get_task(next(iter(factory.store.tasks)))
    task = aggregate.task
    runs = sorted(aggregate.runs, key=lambda value: (value.revision_number, value.role.value))
    attempts = sorted(aggregate.attempts, key=lambda value: str(value.id))
    continuation = []
    initial_run_id = min(runs, key=lambda value: value.queued_at).id
    for message in factory.store.outbox:
        if message.schema_name != "agentmesh.run.requested":
            continue
        payload = message.payload
        if payload.get("run_id") == str(initial_run_id):
            continue
        continuation.append(
            {
                "schema": message.schema_name,
                "version": message.schema_version,
                "causation_present": message.causation_id is not None,
                "idempotency_prefix": message.idempotency_key.split(":", 1)[0],
                "role": next(
                    (run.role.value for run in runs if str(run.id) == payload.get("run_id")),
                    "unknown",
                ),
            }
        )
    return {
        "task": {
            "status": task.status.value,
            "output": task.output,
            "candidate_output": task.candidate_output,
            "latest_review": task.latest_review,
            "error": task.error,
            "revision": task.revision_count,
            "budget": {
                "settled_tokens": task.settled_tokens,
                "reserved_tokens": task.reserved_tokens,
                "settled_cost_micros": task.settled_cost_micros,
                "reserved_cost_micros": task.reserved_cost_micros,
                "exhausted_reason": task.budget_exhausted_reason,
                "policy": (
                    {
                        **{
                            key: value
                            for key, value in task.budget.to_dict().items()
                            if key != "deadline"
                        },
                        "deadline_present": task.budget.deadline is not None,
                    }
                    if task.budget is not None
                    else None
                ),
            },
        },
        "runs": [
            {
                "role": run.role.value,
                "status": run.status.value,
                "revision": run.revision_number,
                "authority": run.runtime_authority,
                "cohort": "pinned" if run.runtime_version_id is not None else "legacy",
                "output": run.output,
                "error": run.error,
            }
            for run in runs
        ],
        "attempts": [
            {
                "status": attempt.status.value,
                "reserved_tokens": attempt.reserved_tokens,
                "settled_tokens": attempt.settled_tokens,
                "settled_cost_micros": attempt.settled_cost_micros,
                "settlement_source": (
                    attempt.budget_settlement_source.value
                    if attempt.budget_settlement_source is not None
                    else None
                ),
                "error": attempt.error,
            }
            for attempt in attempts
        ],
        "continuations": sorted(continuation, key=lambda value: (value["role"], value["version"])),
        "quota": sorted(
            [
                {
                    "scope": factory.store.quota_policies[value.policy_id].scope.value,
                    "project_id": factory.store.quota_policies[value.policy_id].project_id,
                    "released": value.released_at is not None,
                }
                for value in factory.store.quota_reservations.values()
            ],
            key=lambda value: (value["scope"], value["project_id"] or ""),
        ),
        "memory_captures": memory.captures,
        "runtime_audit": {
            "observation_count": len(registry.observations) if registry is not None else 0,
            "conflict_count": len(registry.conflicts) if registry is not None else 0,
        },
    }


def _scenario(name: str) -> _Scenario:
    if name == "accept":
        return _Scenario(name, RunRole.REVIEWER, RuntimePhase.SUCCEEDED, REVIEW_ACCEPT)
    if name == "revise":
        return _Scenario(name, RunRole.REVIEWER, RuntimePhase.SUCCEEDED, REVIEW_REJECT)
    if name == "revision_limit":
        return _Scenario(
            name, RunRole.REVIEWER, RuntimePhase.SUCCEEDED, REVIEW_REJECT, max_revisions=0
        )
    if name == "review_deadline":
        return _Scenario(
            name,
            RunRole.REVIEWER,
            RuntimePhase.SUCCEEDED,
            REVIEW_REJECT,
            deadline_expired=True,
        )
    if name == "budget_wait":
        return _Scenario(name, RunRole.EXECUTOR, RuntimePhase.SUCCEEDED, budget_wait=True)
    if name == "failure":
        return _Scenario(name, RunRole.EXECUTOR, RuntimePhase.FAILED)
    if name == "timeout":
        return _Scenario(name, RunRole.EXECUTOR, RuntimePhase.TIMED_OUT)
    raise AssertionError(name)


@pytest.mark.parametrize("name", [entry["scenario"] for entry in PARITY_FIXTURE_REPORT])
def test_reviewed_legacy_and_managed_business_projections_are_parity_qualified(name: str):
    scenario = _scenario(name)
    legacy = _legacy_case(scenario)
    managed = _managed_case(scenario)
    legacy_projection = _run_projection(*legacy)
    managed_projection = _run_projection(*managed)

    # Authority is intentionally the only expected cohort difference.  All
    # business, accounting, continuation, quota, and Memory fields must match.
    assert legacy_projection["task"] == managed_projection["task"]
    assert [
        {key: value for key, value in run.items() if key not in {"authority", "cohort"}}
        for run in legacy_projection["runs"]
    ] == [
        {key: value for key, value in run.items() if key not in {"authority", "cohort"}}
        for run in managed_projection["runs"]
    ]
    assert legacy_projection["attempts"] == managed_projection["attempts"]
    assert legacy_projection["continuations"] == managed_projection["continuations"]
    assert len(legacy_projection["quota"]) == len(managed_projection["quota"])
    assert all(item["released"] for item in legacy_projection["quota"])
    assert all(item["released"] for item in managed_projection["quota"])
    assert legacy_projection["memory_captures"] == managed_projection["memory_captures"]
    # Runtime evidence is the explicit authority-specific audit difference:
    # legacy has no Runtime observation, while managed records exactly one.
    assert legacy_projection["runtime_audit"] == {
        "observation_count": 0,
        "conflict_count": 0,
    }
    assert managed_projection["runtime_audit"] == {
        "observation_count": 1,
        "conflict_count": 0,
    }

    # Explicit authority/cohort proof: managed is pinned and legacy is not.
    assert all(run["authority"] == "legacy" for run in legacy_projection["runs"])
    assert all(run["cohort"] == "legacy" for run in legacy_projection["runs"])
    assert all(run["authority"] == "managed" for run in managed_projection["runs"])
    assert all(run["cohort"] == "pinned" for run in managed_projection["runs"])
