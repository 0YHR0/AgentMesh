from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from agentmesh.application.authority_cohorts import AuthorityCohort, ContinuationKind
from agentmesh.application.services import RunExecutionService, TaskApplicationService
from agentmesh.domain.budgets import BudgetSettlementSource, TaskBudget
from agentmesh.domain.coordination import CoordinatedPlan, SubtaskSpec, SubtaskStatus
from agentmesh.domain.errors import (
    AgentUnavailable,
    FeatureDisabled,
    InvalidTaskInput,
    InvalidTaskTransition,
)
from agentmesh.domain.messaging import RUN_REQUESTED_SCHEMA
from agentmesh.domain.registry import AgentVersionStatus
from agentmesh.domain.tasks import (
    AttemptStatus,
    RunRole,
    RunStatus,
    TaskAttempt,
    TaskExecutionMode,
    TaskRun,
    TaskStatus,
    utc_now,
)
from agentmesh.features import FeatureGateSet
from agentmesh.orchestration.agent import DeterministicAgentExecutor
from agentmesh.orchestration.workflow import LangGraphWorkflowRunner
from tests.fakes import InMemoryUnitOfWorkFactory


def spec(
    key: str,
    *,
    depends_on: tuple[str, ...] = (),
    preferred_agent_id: str | None = None,
    required_capabilities: tuple[str, ...] = ("general.task",),
) -> SubtaskSpec:
    return SubtaskSpec.create(
        key=key,
        objective=f"Execute {key}",
        input={"key": key},
        depends_on=depends_on,
        preferred_agent_id=preferred_agent_id,
        required_capabilities=required_capabilities,
    )


def run_wakeup(
    uow_factory: InMemoryUnitOfWorkFactory,
    run_id,
):
    return next(
        envelope
        for envelope in reversed(uow_factory.store.outbox)
        if envelope.schema_name == RUN_REQUESTED_SCHEMA
        and envelope.payload["run_id"] == str(run_id)
    )


def start_run_for_plan(uow_factory, task_id, run_id):
    with uow_factory() as uow:
        task = uow.tasks.get(task_id, for_update=True)
        run = uow.runs.get(run_id, for_update=True)
        assert task is not None and run is not None and run.subtask_id is not None
        subtask = uow.subtasks.get(run.subtask_id, for_update=True)
        assert subtask is not None
        at = max(task.updated_at, run.queued_at, subtask.updated_at) + timedelta(seconds=1)
        subtask.start(run.id, at=at)
        run.start(at=at)
        attempt = TaskAttempt.lease(
            run_id=run.id,
            worker_id="plan-worker",
            fencing_token=1,
            lease_expires_at=at + timedelta(minutes=5),
        )
        uow.tasks.save(task)
        uow.subtasks.save(subtask)
        uow.runs.save(run)
        uow.attempts.add(attempt)
        uow.commit()
        return attempt.id


def test_fork_join_dag_runs_dependencies_then_supervisor(
    task_service: TaskApplicationService,
    execution_service: RunExecutionService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    plan = CoordinatedPlan.create(
        (
            spec("research"),
            spec("analysis"),
            spec("report", depends_on=("research", "analysis")),
        ),
        max_concurrency=2,
    )
    created = task_service.create_task(
        "Coordinate a fork and join",
        execution_mode=TaskExecutionMode.COORDINATED,
        coordinated_plan=plan,
    )
    started = task_service.request_run(created.task.id)
    by_key = {subtask.key: subtask for subtask in started.subtasks}
    initial_runs = {run.subtask_id: run for run in started.runs}

    assert started.task.status == TaskStatus.RUNNING
    assert len(initial_runs) == 2
    assert by_key["report"].status == SubtaskStatus.BLOCKED
    assert by_key["report"].current_run_id is None

    for key in ("analysis", "research"):
        run = initial_runs[by_key[key].id]
        assert execution_service.process(run_wakeup(uow_factory, run.id)) is True

    joined = task_service.get_task(created.task.id)
    by_key = {subtask.key: subtask for subtask in joined.subtasks}
    report_run = next(run for run in joined.runs if run.subtask_id == by_key["report"].id)
    assert by_key["report"].status == SubtaskStatus.READY
    assert execution_service.process(run_wakeup(uow_factory, report_run.id)) is True

    awaiting_supervisor = task_service.get_task(created.task.id)
    supervisor = next(run for run in awaiting_supervisor.runs if run.role == RunRole.SUPERVISOR)
    assert awaiting_supervisor.task.status == TaskStatus.RUNNING
    assert execution_service.process(run_wakeup(uow_factory, supervisor.id)) is True

    completed = task_service.get_task(created.task.id)
    assert completed.task.status == TaskStatus.COMPLETED
    assert all(subtask.status == SubtaskStatus.COMPLETED for subtask in completed.subtasks)
    assert len(completed.runs) == 4
    assert len({run.thread_id for run in completed.runs}) == 4
    assert len(completed.attempts) == 4
    report = next(subtask for subtask in completed.subtasks if subtask.key == "report")
    assert report.output is not None
    assert set(report.output["input"]["dependency_outputs"]) == {"analysis", "research"}
    assert completed.task.output is not None
    assert set(completed.task.output["input"]["subtask_outputs"]) == {
        "analysis",
        "report",
        "research",
    }


def test_scheduler_enforces_task_concurrency(
    task_service: TaskApplicationService,
    execution_service: RunExecutionService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    plan = CoordinatedPlan.create((spec("a"), spec("b")), max_concurrency=1)
    created = task_service.create_task(
        "Run roots serially",
        execution_mode=TaskExecutionMode.COORDINATED,
        coordinated_plan=plan,
    )
    started = task_service.request_run(created.task.id)
    assert len(started.runs) == 1
    assert started.runs[0].subtask_id == next(
        subtask.id for subtask in started.subtasks if subtask.key == "a"
    )

    assert execution_service.process(run_wakeup(uow_factory, started.runs[0].id)) is True
    after_first = task_service.get_task(created.task.id)
    assert len(after_first.runs) == 2
    assert sum(run.status in {RunStatus.QUEUED, RunStatus.RUNNING} for run in after_first.runs) == 1


class _CountingCohortResolver:
    def __init__(self):
        self.cohort = None
        self.resolve_calls = 0
        self.create_calls = 0

    def resolve_continuation_cohort_in_uow(self, uow, task):
        self.resolve_calls += 1
        if self.cohort is None:
            self.cohort = AuthorityCohort(
                "legacy", None, "off", task_id=task.id, tenant_id=task.tenant_id
            )
        return self.cohort

    def create_continuation_from_cohort_in_uow(
        self, uow, task, agent_id, *, cohort, kind=ContinuationKind.COORDINATED, **kwargs
    ):
        assert cohort is self.cohort
        assert kind is ContinuationKind.COORDINATED
        self.create_calls += 1
        return TaskRun.request(
            task.id,
            agent_id,
            runtime_authority=cohort.runtime_authority,
            runtime_version_id=cohort.runtime_version_id,
            comparison_mode=cohort.comparison_mode,
            **kwargs,
        )


def test_coordinated_schedule_resolves_one_cohort_for_all_new_runs(
    uow_factory: InMemoryUnitOfWorkFactory,
    registry_service,
) -> None:
    resolver = _CountingCohortResolver()
    service = TaskApplicationService(
        uow_factory,
        agent_id="test-agent",
        tenant_id="test-tenant",
        feature_gates=FeatureGateSet.from_config("full"),
        authority_cohort_resolver=resolver,
    )
    plan = CoordinatedPlan.create((spec("a"), spec("b")), max_concurrency=2)
    task = service.create_task(
        "Resolve a single cohort",
        execution_mode=TaskExecutionMode.COORDINATED,
        coordinated_plan=plan,
    )
    started = service.request_run(task.task.id)
    assert len(started.runs) == 2
    assert resolver.resolve_calls == 1
    assert resolver.create_calls == 2


@pytest.mark.parametrize(
    "specs, message",
    [
        ((spec("a", depends_on=("missing",)), spec("b")), "missing dependencies"),
        ((spec("a", depends_on=("b",)), spec("b", depends_on=("a",))), "acyclic"),
        ((spec("a"), spec("a")), "unique"),
    ],
)
def test_plan_validation_rejects_invalid_dags(specs, message: str) -> None:
    with pytest.raises(InvalidTaskInput, match=message):
        CoordinatedPlan.create(specs, max_concurrency=2)


def test_capability_mismatch_rolls_back_coordination_start(
    task_service: TaskApplicationService,
) -> None:
    plan = CoordinatedPlan.create(
        (
            spec("a", preferred_agent_id="test-reviewer"),
            spec("b"),
        ),
        max_concurrency=2,
    )
    created = task_service.create_task(
        "Reject an invalid assignment",
        execution_mode=TaskExecutionMode.COORDINATED,
        coordinated_plan=plan,
    )

    with pytest.raises(AgentUnavailable, match="does not satisfy"):
        task_service.request_run(created.task.id)

    persisted = task_service.get_task(created.task.id)
    assert persisted.task.status == TaskStatus.CREATED
    assert persisted.runs == []


class _FailFirstExecutor(DeterministicAgentExecutor):
    def execute(self, *, objective, input, context):
        if objective == "Execute a":
            raise RuntimeError("deterministic failure")
        return super().execute(objective=objective, input=input, context=context)


def test_failed_subtask_fails_task_and_cancels_siblings(
    task_service: TaskApplicationService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    execution_service = RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=LangGraphWorkflowRunner(
            agent_executor=_FailFirstExecutor(),
            checkpointer=InMemorySaver(),
        ),
        worker_id="coordinated-failure-worker",
        consumer_name="coordinated-failure-worker-v1",
        lease_duration=timedelta(minutes=5),
        supervisor_agent_id="test-supervisor",
    )
    plan = CoordinatedPlan.create(
        (spec("a"), spec("b"), spec("join", depends_on=("a", "b"))),
        max_concurrency=2,
    )
    created = task_service.create_task(
        "Fail the coordinated plan",
        execution_mode=TaskExecutionMode.COORDINATED,
        coordinated_plan=plan,
    )
    started = task_service.request_run(created.task.id)
    a = next(subtask for subtask in started.subtasks if subtask.key == "a")
    a_run = next(run for run in started.runs if run.subtask_id == a.id)

    assert execution_service.process(run_wakeup(uow_factory, a_run.id)) is True

    failed = task_service.get_task(created.task.id)
    assert failed.task.status == TaskStatus.FAILED
    assert {subtask.status for subtask in failed.subtasks} == {
        SubtaskStatus.FAILED,
        SubtaskStatus.CANCELED,
    }
    assert all(run.status in {RunStatus.FAILED, RunStatus.CANCELED} for run in failed.runs)


@pytest.mark.parametrize("failure", [False, True], ids=["budget-rejection", "failure"])
def test_legacy_coordinated_worker_accounts_for_siblings(
    task_service: TaskApplicationService,
    execution_service: RunExecutionService,
    uow_factory: InMemoryUnitOfWorkFactory,
    failure: bool,
) -> None:
    budget = TaskBudget.create(
        max_tokens=100,
        token_reservation_per_attempt=2,
        deadline=utc_now() + timedelta(minutes=5),
    )
    created = task_service.create_task(
        "Reject coordinated continuation after settlement",
        execution_mode=TaskExecutionMode.COORDINATED,
        coordinated_plan=CoordinatedPlan.create(
            (spec("a"), spec("b")),
            max_concurrency=2,
        ),
        budget=budget,
    )
    started = task_service.request_run(created.task.id)
    runs = sorted(started.runs, key=lambda item: str(item.id))
    envelopes = {
        item.payload["run_id"]: item
        for item in uow_factory.store.outbox
        if item.payload.get("task_id") == str(created.task.id)
    }
    leased = [
        execution_service._acquire(envelopes[str(run.id)], task_id=created.task.id, run_id=run.id)
        for run in runs
    ]
    assert all(item is not None for item in leased)

    target = leased[0]
    assert target is not None
    envelope = envelopes[str(runs[0].id)]
    if failure:
        execution_service._finalize_failure(
            envelope,
            created.task.id,
            runs[0].id,
            target[2].id,
            "target failed",
        )
    else:
        with uow_factory() as uow:
            task = uow.tasks.get(created.task.id, for_update=True)
            assert task is not None and task.budget is not None
            task.budget = replace(task.budget, deadline=utc_now() - timedelta(seconds=1))
            uow.tasks.save(task)
            uow.commit()
        execution_service._finalize_success(
            envelope,
            created.task.id,
            runs[0].id,
            target[2].id,
            {"result": "target"},
        )

    completed = task_service.get_task(created.task.id)
    assert completed.task.status is (TaskStatus.FAILED if failure else TaskStatus.WAITING_APPROVAL)
    terminal_statuses = (
        {RunStatus.FAILED, RunStatus.CANCELED}
        if failure
        else {RunStatus.SUCCEEDED, RunStatus.CANCELED}
    )
    assert all(item.status in terminal_statuses for item in completed.runs)
    by_run = {item.run_id: item for item in completed.attempts}
    target_attempt = by_run[runs[0].id]
    sibling_attempt = by_run[runs[1].id]
    assert target_attempt.status is (AttemptStatus.FAILED if failure else AttemptStatus.SUCCEEDED)
    assert target_attempt.budget_settlement_source is (
        BudgetSettlementSource.RELEASED if failure else BudgetSettlementSource.CONSERVATIVE_ESTIMATE
    )
    assert sibling_attempt.status is AttemptStatus.CANCELED
    assert sibling_attempt.budget_settlement_source is BudgetSettlementSource.RELEASED


def test_coordinated_execution_is_disabled_in_minimal_profile(
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    service = TaskApplicationService(
        uow_factory,
        agent_id="test-agent",
        tenant_id="test-tenant",
        feature_gates=FeatureGateSet.from_config("minimal"),
    )
    with pytest.raises(FeatureDisabled):
        service.create_task(
            "Disabled plan",
            execution_mode=TaskExecutionMode.COORDINATED,
            coordinated_plan=CoordinatedPlan.create((spec("a"), spec("b")), max_concurrency=1),
        )


def test_scheduler_plan_is_read_only_then_applies_hypothetical_successor(
    task_service, uow_factory
) -> None:
    plan = CoordinatedPlan.create(
        (spec("first"), spec("last", depends_on=("first",))), max_concurrency=1
    )
    aggregate = task_service.create_task(
        "Plan a successor",
        execution_mode=TaskExecutionMode.COORDINATED,
        coordinated_plan=plan,
    )
    started = task_service.request_run(aggregate.task.id)
    target_run = started.runs[0]
    attempt_id = start_run_for_plan(uow_factory, aggregate.task.id, target_run.id)
    scheduler = task_service._coordinated_scheduler
    causation_id = uuid4()
    with uow_factory() as uow:
        task = uow.tasks.get(aggregate.task.id, for_update=True)
        run = uow.runs.get(target_run.id, for_update=True)
        subtask = uow.subtasks.get(target_run.subtask_id, for_update=True)
        attempt = uow.attempts.get(attempt_id, for_update=True)
        assert task is not None and run is not None and subtask is not None and attempt is not None
        at = max(task.updated_at, run.started_at, subtask.updated_at, attempt.heartbeat_at)
        at += timedelta(seconds=1)
        outbox_count = len(uow.outbox._outbox)
        uow.tasks.save = lambda value: (_ for _ in ()).throw(AssertionError("plan wrote Task"))
        uow.subtasks.save = lambda value: (_ for _ in ()).throw(
            AssertionError("plan wrote Subtask")
        )
        uow.runs.add = lambda value: (_ for _ in ()).throw(AssertionError("plan added Run"))
        uow.outbox.add = lambda value: (_ for _ in ()).throw(AssertionError("plan wrote Outbox"))
        planned = scheduler.plan(
            uow,
            task,
            completing_subtask_id=subtask.id,
            completion_output={"ok": True},
            at=at,
            causation_id=causation_id,
        )
        assert len(planned.planned_runs) == 1
        assert len(uow.outbox._outbox) == outbox_count

    with uow_factory() as uow:
        task = uow.tasks.get(aggregate.task.id, for_update=True)
        run = uow.runs.get(target_run.id, for_update=True)
        subtask = uow.subtasks.get(target_run.subtask_id, for_update=True)
        attempt = uow.attempts.get(attempt_id, for_update=True)
        assert task is not None and run is not None and subtask is not None and attempt is not None
        subtask.complete(run.id, {"ok": True}, at=planned.at)
        run.succeed({"ok": True}, at=planned.at)
        attempt.succeed(at=planned.at)
        uow.subtasks.save(subtask)
        uow.runs.save(run)
        uow.attempts.save(attempt)
        created = scheduler.apply(uow, task, planned)
        assert len(created) == 1
        envelope = uow.outbox._outbox[-1]
        assert envelope.causation_id == causation_id
        assert envelope.occurred_at == planned.at


def test_scheduler_plan_last_subtask_creates_supervisor_without_writes(
    task_service, uow_factory
) -> None:
    plan = CoordinatedPlan.create((spec("a"), spec("b")), max_concurrency=2)
    aggregate = task_service.create_task(
        "Plan supervisor",
        execution_mode=TaskExecutionMode.COORDINATED,
        coordinated_plan=plan,
    )
    started = task_service.request_run(aggregate.task.id)
    target = started.runs[0]
    attempt_id = start_run_for_plan(uow_factory, aggregate.task.id, target.id)
    with uow_factory() as uow:
        task = uow.tasks.get(aggregate.task.id, for_update=True)
        run = uow.runs.get(target.id, for_update=True)
        subtask = uow.subtasks.get(target.subtask_id, for_update=True)
        attempt = uow.attempts.get(attempt_id, for_update=True)
        assert task is not None and run is not None and subtask is not None and attempt is not None
        # Make the sibling complete so this target is the final barrier node.
        sibling_run = next(value for value in started.runs if value.id != target.id)
        sibling_subtask = next(
            value for value in started.subtasks if value.id == sibling_run.subtask_id
        )
        sibling_attempt_id = start_run_for_plan(uow_factory, aggregate.task.id, sibling_run.id)
        # The helper opens its own UoW; reload all values after that commit.
    with uow_factory() as uow:
        task = uow.tasks.get(aggregate.task.id, for_update=True)
        run = uow.runs.get(target.id, for_update=True)
        subtask = uow.subtasks.get(target.subtask_id, for_update=True)
        attempt = uow.attempts.get(attempt_id, for_update=True)
        sibling_run = uow.runs.get(sibling_run.id, for_update=True)
        sibling_subtask = uow.subtasks.get(sibling_subtask.id, for_update=True)
        sibling_attempt = uow.attempts.get(sibling_attempt_id, for_update=True)
        assert all(
            value is not None
            for value in (
                task,
                run,
                subtask,
                attempt,
                sibling_run,
                sibling_subtask,
                sibling_attempt,
            )
        )
        at = max(task.updated_at, run.started_at, subtask.updated_at, attempt.heartbeat_at)
        at = max(
            at, sibling_run.started_at, sibling_subtask.updated_at, sibling_attempt.heartbeat_at
        ) + timedelta(seconds=1)
        sibling_subtask.complete(sibling_run.id, {"sibling": True}, at=at)
        sibling_run.succeed({"sibling": True}, at=at)
        sibling_attempt.succeed(at=at)
        uow.subtasks.save(sibling_subtask)
        uow.runs.save(sibling_run)
        uow.attempts.save(sibling_attempt)
        scheduler = task_service._coordinated_scheduler
        planned = scheduler.plan(
            uow,
            task,
            completing_subtask_id=subtask.id,
            completion_output={"target": True},
            at=at,
        )
        assert planned.planned_runs[0].role is RunRole.SUPERVISOR


def test_scheduler_plan_budget_hold_has_no_run_and_apply_waits(task_service, uow_factory) -> None:
    budget = TaskBudget.create(max_runs=1)
    plan = CoordinatedPlan.create((spec("a"), spec("b")), max_concurrency=1)
    aggregate = task_service.create_task(
        "Budget hold",
        execution_mode=TaskExecutionMode.COORDINATED,
        coordinated_plan=plan,
        budget=budget,
    )
    started = task_service.request_run(aggregate.task.id)
    first = started.runs[0]
    attempt_id = start_run_for_plan(uow_factory, aggregate.task.id, first.id)
    with uow_factory() as uow:
        task = uow.tasks.get(aggregate.task.id, for_update=True)
        run = uow.runs.get(first.id, for_update=True)
        subtask = uow.subtasks.get(first.subtask_id, for_update=True)
        attempt = uow.attempts.get(attempt_id, for_update=True)
        assert task is not None and run is not None and subtask is not None and attempt is not None
        at = max(task.updated_at, run.started_at, subtask.updated_at, attempt.heartbeat_at)
        at += timedelta(seconds=1)
        subtask.complete(run.id, {"ok": True}, at=at)
        run.succeed({"ok": True}, at=at)
        attempt.succeed(at=at)
        uow.subtasks.save(subtask)
        uow.runs.save(run)
        uow.attempts.save(attempt)
        uow.commit()
    scheduler = task_service._coordinated_scheduler
    with uow_factory() as uow:
        task = uow.tasks.get(aggregate.task.id, for_update=True)
        assert task is not None
        planned = scheduler.plan(uow, task, at=at)
        assert planned.planned_runs == ()
        assert planned.wait_for_budget is True
        scheduler.apply(uow, task, planned)
        persisted = uow.tasks.get(task.id)
        assert persisted is not None and persisted.status is TaskStatus.WAITING_APPROVAL


def test_scheduler_apply_rejects_stale_token_before_writes(task_service, uow_factory) -> None:
    plan = CoordinatedPlan.create((spec("a"), spec("b")), max_concurrency=1)
    aggregate = task_service.create_task(
        "Stale coordination plan",
        execution_mode=TaskExecutionMode.COORDINATED,
        coordinated_plan=plan,
    )
    started = task_service.request_run(aggregate.task.id)
    scheduler = task_service._coordinated_scheduler
    with uow_factory() as uow:
        task = uow.tasks.get(aggregate.task.id, for_update=True)
        assert task is not None
        at = task.updated_at + timedelta(seconds=1)
        planned = scheduler.plan(uow, task, at=at)
    with uow_factory() as uow:
        run = uow.runs.get(started.runs[0].id, for_update=True)
        assert run is not None
        run.start(at=at)
        uow.runs.save(run)
        uow.commit()
    with uow_factory() as uow:
        task = uow.tasks.get(aggregate.task.id, for_update=True)
        assert task is not None
        saves: list[str] = []
        uow.tasks.save = lambda value: saves.append("task")
        uow.subtasks.save = lambda value: saves.append("subtask")
        uow.runs.add = lambda value: saves.append("run")
        uow.outbox.add = lambda value: saves.append("outbox")
        with pytest.raises(InvalidTaskTransition, match="stale"):
            scheduler.apply(uow, task, planned)
        assert saves == []


def test_scheduler_plan_agent_unavailable_is_zero_write(task_service, uow_factory) -> None:
    plan = CoordinatedPlan.create(
        (spec("a", preferred_agent_id="missing-agent"), spec("b")), max_concurrency=1
    )
    aggregate = task_service.create_task(
        "Unavailable coordinated agent",
        execution_mode=TaskExecutionMode.COORDINATED,
        coordinated_plan=plan,
    )
    with uow_factory() as uow:
        task = uow.tasks.get(aggregate.task.id, for_update=True)
        assert task is not None
        task.start_coordination()
        uow.tasks.save(task)
        uow.commit()
    scheduler = task_service._coordinated_scheduler
    with uow_factory() as uow:
        task = uow.tasks.get(aggregate.task.id, for_update=True)
        assert task is not None
        saves: list[str] = []
        uow.tasks.save = lambda value: saves.append("task")
        uow.subtasks.save = lambda value: saves.append("subtask")
        uow.runs.add = lambda value: saves.append("run")
        uow.outbox.add = lambda value: saves.append("outbox")
        with pytest.raises(AgentUnavailable):
            scheduler.plan(uow, task, at=task.updated_at + timedelta(seconds=1))
        assert saves == []


def test_scheduler_plan_revoked_agent_is_zero_write(task_service, uow_factory) -> None:
    plan = CoordinatedPlan.create(
        (spec("a", preferred_agent_id="test-agent"), spec("b")), max_concurrency=1
    )
    aggregate = task_service.create_task(
        "Revoked coordinated agent",
        execution_mode=TaskExecutionMode.COORDINATED,
        coordinated_plan=plan,
    )
    with uow_factory() as uow:
        task = uow.tasks.get(aggregate.task.id, for_update=True)
        assert task is not None
        task.start_coordination()
        definition = uow.agent_definitions.get_by_name("test-tenant", "test-agent")
        assert definition is not None and definition.default_version_id is not None
        version = uow.agent_versions.get(definition.default_version_id)
        assert version is not None
        version.revoke("revoked for planner test")
        uow.tasks.save(task)
        uow.agent_versions.save(version)
        uow.commit()
    scheduler = task_service._coordinated_scheduler
    with uow_factory() as uow:
        task = uow.tasks.get(aggregate.task.id, for_update=True)
        assert task is not None
        saves: list[str] = []
        uow.tasks.save = lambda value: saves.append("task")
        uow.subtasks.save = lambda value: saves.append("subtask")
        uow.runs.add = lambda value: saves.append("run")
        uow.outbox.add = lambda value: saves.append("outbox")
        with pytest.raises(AgentUnavailable):
            scheduler.plan(uow, task, at=task.updated_at + timedelta(seconds=1))
        assert saves == []


def test_scheduler_non_running_compatibility_returns_empty(task_service, uow_factory) -> None:
    aggregate = task_service.create_task("Not started")
    with uow_factory() as uow:
        task = uow.tasks.get(aggregate.task.id, for_update=True)
        assert task is not None
        saves: list[str] = []
        uow.tasks.save = lambda value: saves.append("task")
        assert task_service._coordinated_scheduler.schedule(uow, task) == []
        assert saves == []


def test_scheduler_malformed_plan_is_rejected_before_writes(task_service, uow_factory) -> None:
    plan = CoordinatedPlan.create((spec("a"), spec("b")), max_concurrency=1)
    aggregate = task_service.create_task(
        "Malformed coordination plan",
        execution_mode=TaskExecutionMode.COORDINATED,
        coordinated_plan=plan,
    )
    task_service.request_run(aggregate.task.id)
    scheduler = task_service._coordinated_scheduler
    with uow_factory() as uow:
        task = uow.tasks.get(aggregate.task.id, for_update=True)
        assert task is not None
        planned = scheduler.plan(uow, task, at=task.updated_at + timedelta(seconds=1))
    malformed = replace(planned, ready_subtask_ids=(uuid4(),))
    with uow_factory() as uow:
        task = uow.tasks.get(aggregate.task.id, for_update=True)
        assert task is not None
        saves: list[str] = []
        uow.tasks.save = lambda value: saves.append("task")
        uow.subtasks.save = lambda value: saves.append("subtask")
        uow.runs.add = lambda value: saves.append("run")
        uow.outbox.add = lambda value: saves.append("outbox")
        with pytest.raises(InvalidTaskTransition):
            scheduler.apply(uow, task, malformed)
        assert saves == []


@pytest.mark.parametrize("kind", ["not_tuple", "wrong_length", "duplicate"])
def test_scheduler_malformed_run_snapshot_is_rejected_before_writes(
    task_service, uow_factory, kind
) -> None:
    plan = CoordinatedPlan.create((spec("a"), spec("b")), max_concurrency=1)
    aggregate = task_service.create_task(
        "Malformed Run token",
        execution_mode=TaskExecutionMode.COORDINATED,
        coordinated_plan=plan,
    )
    task_service.request_run(aggregate.task.id)
    scheduler = task_service._coordinated_scheduler
    with uow_factory() as uow:
        task = uow.tasks.get(aggregate.task.id, for_update=True)
        assert task is not None
        planned = scheduler.plan(uow, task, at=task.updated_at + timedelta(seconds=1))
    if kind == "not_tuple":
        malformed_snapshot = list(planned.run_snapshot)
    elif kind == "wrong_length":
        malformed_snapshot = (planned.run_snapshot[0][:-1],)
    else:
        malformed_snapshot = planned.run_snapshot + planned.run_snapshot
    malformed = replace(planned, run_snapshot=malformed_snapshot)
    with uow_factory() as uow:
        task = uow.tasks.get(aggregate.task.id, for_update=True)
        assert task is not None
        before_task = deepcopy(task)
        before_subtasks = deepcopy(uow.subtasks.list_for_task(aggregate.task.id, for_update=True))
        before_runs = deepcopy(uow.runs.list_for_task(aggregate.task.id))
        before_outbox = deepcopy(uow.outbox._outbox)
        saves: list[str] = []
        uow.tasks.save = lambda value: saves.append("task")
        uow.subtasks.save = lambda value: saves.append("subtask")
        uow.runs.add = lambda value: saves.append("run")
        uow.outbox.add = lambda value: saves.append("outbox")
        with pytest.raises(InvalidTaskTransition):
            scheduler.apply(uow, task, malformed)
        assert saves == []
        assert task == before_task
        assert uow.subtasks.list_for_task(aggregate.task.id) == before_subtasks
        assert uow.runs.list_for_task(aggregate.task.id) == before_runs
        assert uow.outbox._outbox == before_outbox


def _build_hypothetical_successor_plan(task_service, uow_factory, title):
    plan = CoordinatedPlan.create(
        (spec("first"), spec("last", depends_on=("first",))), max_concurrency=1
    )
    aggregate = task_service.create_task(
        title,
        execution_mode=TaskExecutionMode.COORDINATED,
        coordinated_plan=plan,
    )
    started = task_service.request_run(aggregate.task.id)
    target_run = started.runs[0]
    attempt_id = start_run_for_plan(uow_factory, aggregate.task.id, target_run.id)
    scheduler = task_service._coordinated_scheduler
    with uow_factory() as uow:
        task = uow.tasks.get(aggregate.task.id, for_update=True)
        run = uow.runs.get(target_run.id, for_update=True)
        subtask = uow.subtasks.get(target_run.subtask_id, for_update=True)
        attempt = uow.attempts.get(attempt_id, for_update=True)
        assert task is not None and run is not None and subtask is not None and attempt is not None
        at = max(task.updated_at, run.started_at, subtask.updated_at, attempt.heartbeat_at)
        planned = scheduler.plan(
            uow,
            task,
            completing_subtask_id=subtask.id,
            completion_output={"ok": True},
            at=at + timedelta(seconds=1),
        )
    assert len(planned.planned_runs) == 1
    return aggregate.task.id, target_run.id, attempt_id, planned, scheduler


def _complete_hypothetical_target(uow_factory, task_id, run_id, attempt_id, at):
    with uow_factory() as uow:
        run = uow.runs.get(run_id, for_update=True)
        task = uow.tasks.get(task_id, for_update=True)
        assert run is not None and task is not None and run.subtask_id is not None
        subtask = uow.subtasks.get(run.subtask_id, for_update=True)
        attempt = uow.attempts.get(attempt_id, for_update=True)
        assert subtask is not None and attempt is not None
        subtask.complete(run.id, {"ok": True}, at=at)
        run.succeed({"ok": True}, at=at)
        attempt.succeed(at=at)
        uow.subtasks.save(subtask)
        uow.runs.save(run)
        uow.attempts.save(attempt)
        uow.commit()


@pytest.mark.parametrize(
    "field",
    [
        "id",
        "agent_id",
        "role",
        "subtask_id",
        "agent_version_id",
        "agent_version_digest",
        "thread_id",
        "revision_number",
    ],
)
def test_scheduler_tampered_planned_run_is_rejected_before_writes(
    task_service, uow_factory, field
) -> None:
    (
        valid_task_id,
        valid_target_run_id,
        valid_attempt_id,
        valid_plan,
        scheduler,
    ) = _build_hypothetical_successor_plan(
        task_service, uow_factory, "Valid hypothetical coordination plan"
    )
    valid_at = valid_plan.at
    _complete_hypothetical_target(
        uow_factory, valid_task_id, valid_target_run_id, valid_attempt_id, valid_at
    )
    with uow_factory() as uow:
        task = uow.tasks.get(valid_task_id, for_update=True)
        assert task is not None
        created = scheduler.apply(uow, task, valid_plan)
        assert len(created) == 1

    (
        task_id,
        target_run_id,
        attempt_id,
        planned,
        scheduler,
    ) = _build_hypothetical_successor_plan(task_service, uow_factory, "Tampered coordination plan")
    _complete_hypothetical_target(uow_factory, task_id, target_run_id, attempt_id, planned.at)
    original = planned.planned_runs[0]
    tampered_value = {
        "id": uuid4(),
        "agent_id": "forged-agent",
        "role": RunRole.SUPERVISOR,
        "subtask_id": uuid4(),
        "agent_version_id": uuid4(),
        "agent_version_digest": "forged-digest",
        "thread_id": "forged-thread",
        "revision_number": original.revision_number + 1,
    }[field]
    tampered = replace(original, **{field: tampered_value})
    malformed = replace(planned, planned_runs=(tampered,))
    with uow_factory() as uow:
        task = uow.tasks.get(task_id, for_update=True)
        assert task is not None
        before_task = deepcopy(task)
        before_subtasks = deepcopy(uow.subtasks.list_for_task(task_id, for_update=True))
        before_runs = deepcopy(uow.runs.list_for_task(task_id))
        before_outbox = deepcopy(uow.outbox._outbox)
        saves: list[str] = []
        uow.tasks.save = lambda value: saves.append("task")
        uow.subtasks.save = lambda value: saves.append("subtask")
        uow.runs.add = lambda value: saves.append("run")
        uow.outbox.add = lambda value: saves.append("outbox")
        with pytest.raises(InvalidTaskTransition):
            scheduler.apply(uow, task, malformed)
        assert saves == []
        assert task == before_task
        assert uow.subtasks.list_for_task(task_id) == before_subtasks
        assert uow.runs.list_for_task(task_id) == before_runs
        assert uow.outbox._outbox == before_outbox


@pytest.mark.parametrize("mutation", ["archive", "draft"])
def test_scheduler_apply_rejects_non_publishable_planned_agent_before_writes(
    task_service, uow_factory, mutation
) -> None:
    task_id, target_run_id, attempt_id, planned, scheduler = _build_hypothetical_successor_plan(
        task_service, uow_factory, f"Invalid planned agent: {mutation}"
    )
    _complete_hypothetical_target(uow_factory, task_id, target_run_id, attempt_id, planned.at)
    with uow_factory() as uow:
        planned_run = planned.planned_runs[0]
        definition = uow.agent_definitions.get_by_name("test-tenant", planned_run.agent_id)
        assert definition is not None
        if mutation == "archive":
            definition.archive()
            uow.agent_definitions.save(definition)
        else:
            assert definition.default_version_id == planned_run.agent_version_id
            version = uow.agent_versions.get(definition.default_version_id)
            assert version is not None
            version.status = AgentVersionStatus.DRAFT
            uow.agent_versions.save(version)
        uow.commit()

    with uow_factory() as uow:
        task = uow.tasks.get(task_id, for_update=True)
        assert task is not None
        before_task = deepcopy(task)
        before_subtasks = deepcopy(uow.subtasks.list_for_task(task_id, for_update=True))
        before_runs = deepcopy(uow.runs.list_for_task(task_id))
        before_outbox = deepcopy(uow.outbox._outbox)
        saves: list[str] = []
        uow.tasks.save = lambda value: saves.append("task")
        uow.subtasks.save = lambda value: saves.append("subtask")
        uow.runs.add = lambda value: saves.append("run")
        uow.outbox.add = lambda value: saves.append("outbox")
        with pytest.raises(InvalidTaskTransition, match="Planned Agent"):
            scheduler.apply(uow, task, planned)
        assert saves == []
        assert task == before_task
        assert uow.subtasks.list_for_task(task_id) == before_subtasks
        assert uow.runs.list_for_task(task_id) == before_runs
        assert uow.outbox._outbox == before_outbox
