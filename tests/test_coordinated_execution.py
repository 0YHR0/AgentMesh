from __future__ import annotations

from datetime import timedelta

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from agentmesh.application.services import RunExecutionService, TaskApplicationService
from agentmesh.domain.coordination import CoordinatedPlan, SubtaskSpec, SubtaskStatus
from agentmesh.domain.errors import AgentUnavailable, FeatureDisabled, InvalidTaskInput
from agentmesh.domain.messaging import RUN_REQUESTED_SCHEMA
from agentmesh.domain.tasks import RunRole, RunStatus, TaskExecutionMode, TaskStatus
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
            coordinated_plan=CoordinatedPlan.create(
                (spec("a"), spec("b")), max_concurrency=1
            ),
        )
