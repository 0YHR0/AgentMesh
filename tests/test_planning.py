from uuid import uuid4

import pytest

from agentmesh.application.planning_services import PlanningApplicationService
from agentmesh.application.services import TaskApplicationService
from agentmesh.domain.coordination import CoordinatedPlan, SubtaskSpec
from agentmesh.domain.errors import FeatureDisabled, InvalidTaskInput, InvalidTaskTransition
from agentmesh.domain.planning import GoalContract, PlanPatchStatus
from agentmesh.domain.tasks import TaskExecutionMode
from agentmesh.domain.tools import ToolBinding, ToolInvocation, ToolSideEffect
from agentmesh.features import FeatureGateSet
from tests.fakes import InMemoryUnitOfWorkFactory


def spec(key: str, *, depends_on: tuple[str, ...] = (), objective: str | None = None):
    return SubtaskSpec.create(
        key=key,
        objective=objective or f"Complete {key}",
        depends_on=depends_on,
    )


def create_coordinated_task(service: TaskApplicationService):
    return service.create_task(
        "Deliver a verified market brief",
        execution_mode=TaskExecutionMode.COORDINATED,
        coordinated_plan=CoordinatedPlan.create(
            (spec("research"), spec("synthesize", depends_on=("research",))),
            max_concurrency=1,
        ),
        goal_constraints=("Use traceable evidence",),
        goal_success_criteria=("Produce one decision-ready recommendation",),
    )


def reach_budget_barrier(
    task_service: TaskApplicationService,
    uow_factory: InMemoryUnitOfWorkFactory,
):
    aggregate = create_coordinated_task(task_service)
    task_service.request_run(aggregate.task.id)
    with uow_factory() as uow:
        task = uow.tasks.get(aggregate.task.id, for_update=True)
        run = uow.runs.list_for_task(task.id)[0]
        subtask = uow.subtasks.get(run.subtask_id, for_update=True)
        run.start()
        subtask.start(run.id)
        run.succeed({"evidence": "preserved"})
        subtask.complete(run.id, {"evidence": "preserved"})
        task.wait_for_budget("max_runs_exhausted")
        uow.runs.save(run)
        uow.subtasks.save(subtask)
        uow.tasks.save(task)
        uow.commit()
    return task_service.get_task(task.id), run


def test_goal_contract_is_canonical_and_bounded() -> None:
    task_id = uuid4()
    first = GoalContract.create(
        task_id=task_id,
        objective="Ship the result",
        constraints=("No secrets",),
        success_criteria=("Result is reviewable",),
    )
    second = GoalContract.create(
        task_id=task_id,
        objective=" Ship the result ",
        constraints=("No secrets",),
        success_criteria=("Result is reviewable",),
    )

    assert first.digest == second.digest
    with pytest.raises(InvalidTaskInput, match="must be unique"):
        GoalContract.create(
            task_id=task_id,
            objective="Ship",
            constraints=("same", "same"),
        )


def test_verified_plan_patch_applies_atomically_before_execution(
    task_service: TaskApplicationService,
    planning_service: PlanningApplicationService,
) -> None:
    aggregate = create_coordinated_task(task_service)
    task = aggregate.task

    patch = planning_service.propose_patch(
        task.id,
        base_plan_version=task.plan_version or 0,
        base_plan_digest=task.plan_digest or "",
        specs=(
            spec("research"),
            spec("analyze", depends_on=("research",)),
            spec("synthesize", depends_on=("analyze",)),
        ),
        max_concurrency=2,
        reason="Add an independent analysis stage",
        requested_by="operator-a",
    )

    assert patch.status is PlanPatchStatus.VERIFIED
    assert all(finding.passed for finding in patch.evidence)
    applied = planning_service.apply_patch(task.id, patch.id)
    assert applied.status is PlanPatchStatus.APPLIED

    updated = task_service.get_task(task.id)
    assert updated.task.plan_version == 2
    assert updated.task.plan_digest == patch.proposed_plan_digest
    assert updated.task.max_concurrency == 2
    assert [subtask.key for subtask in updated.subtasks] == [
        "analyze",
        "research",
        "synthesize",
    ]
    snapshot = planning_service.get_snapshot(task.id)
    assert snapshot.goal.constraints == ("Use traceable evidence",)
    assert snapshot.patches[0].status is PlanPatchStatus.APPLIED
    task_service.request_run(task.id)
    assert planning_service.apply_patch(task.id, patch.id).applied_at == applied.applied_at


def test_plan_patch_rejects_stale_noop_and_execution_history(
    task_service: TaskApplicationService,
    planning_service: PlanningApplicationService,
) -> None:
    aggregate = create_coordinated_task(task_service)
    task = aggregate.task
    original = (spec("research"), spec("synthesize", depends_on=("research",)))

    with pytest.raises(InvalidTaskTransition, match="plan-changed"):
        planning_service.propose_patch(
            task.id,
            base_plan_version=1,
            base_plan_digest=task.plan_digest or "",
            specs=original,
            max_concurrency=1,
            reason="No actual change",
            requested_by="operator-a",
        )
    with pytest.raises(InvalidTaskTransition, match="base-version-current"):
        planning_service.propose_patch(
            task.id,
            base_plan_version=99,
            base_plan_digest="sha256:stale",
            specs=(spec("research-updated"), spec("synthesize", depends_on=("research-updated",))),
            max_concurrency=1,
            reason="Stale proposal",
            requested_by="operator-a",
        )

    task_service.request_run(task.id)
    with pytest.raises(InvalidTaskTransition, match="quiescent WAITING_APPROVAL"):
        planning_service.propose_patch(
            task.id,
            base_plan_version=1,
            base_plan_digest=task.plan_digest or "",
            specs=(spec("new-a"), spec("new-b", depends_on=("new-a",))),
            max_concurrency=1,
            reason="Too late",
            requested_by="operator-a",
        )


def test_quiescent_running_patch_preserves_completed_work_and_replaces_only_unstarted(
    task_service: TaskApplicationService,
    planning_service: PlanningApplicationService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    waiting, _run = reach_budget_barrier(task_service, uow_factory)
    task = waiting.task
    completed = next(item for item in waiting.subtasks if item.key == "research")

    patch = planning_service.propose_patch(
        task.id,
        base_plan_version=task.plan_version or 0,
        base_plan_digest=task.plan_digest or "",
        specs=(spec("research"), spec("publish", depends_on=("research",))),
        max_concurrency=1,
        reason="Replace the unstarted synthesis step at the budget barrier",
        requested_by="operator-a",
    )
    evidence = {finding.code: finding for finding in patch.evidence}

    assert evidence["execution-history-safe"].details["mode"] == "quiescent-running"
    applied = planning_service.apply_patch(task.id, patch.id)
    updated = task_service.get_task(task.id)
    preserved = next(item for item in updated.subtasks if item.key == "research")
    replacement = next(item for item in updated.subtasks if item.key == "publish")

    assert applied.status is PlanPatchStatus.APPLIED
    assert updated.task.status.value == "WAITING_APPROVAL"
    assert updated.task.plan_version == 2
    assert {item.key for item in updated.subtasks} == {"research", "publish"}
    assert preserved.id == completed.id
    assert preserved.output == {"evidence": "preserved"}
    assert replacement.status.value == "READY"
    assert uow_factory.store.outbox[-1].schema_name == "agentmesh.task.plan-patch-applied"


def test_quiescent_running_patch_rejects_completed_rewrite_and_write_tool_history(
    task_service: TaskApplicationService,
    planning_service: PlanningApplicationService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    waiting, run = reach_budget_barrier(task_service, uow_factory)
    task = waiting.task
    with pytest.raises(InvalidTaskTransition, match="preserve completed Subtask research"):
        planning_service.propose_patch(
            task.id,
            base_plan_version=task.plan_version or 0,
            base_plan_digest=task.plan_digest or "",
            specs=(
                spec("research", objective="Rewrite completed work"),
                spec("publish", depends_on=("research",)),
            ),
            max_concurrency=1,
            reason="Unsafe rewrite",
            requested_by="operator-a",
        )

    invocation = ToolInvocation.start(
        tenant_id="test-tenant",
        task_id=task.id,
        run_id=run.id,
        binding=ToolBinding(
            logical_key="workspace.write_text",
            server_name="workspace",
            tool_name="write_text",
            side_effect=ToolSideEffect.IDEMPOTENT_WRITE,
        ),
        arguments={"idempotency_key": "write-1"},
    )
    with uow_factory() as uow:
        uow.tool_invocations.add(invocation)
        uow.commit()

    with pytest.raises(InvalidTaskTransition, match="write-class Tool history"):
        planning_service.propose_patch(
            task.id,
            base_plan_version=task.plan_version or 0,
            base_plan_digest=task.plan_digest or "",
            specs=(spec("research"), spec("publish", depends_on=("research",))),
            max_concurrency=1,
            reason="Blocked by write history",
            requested_by="operator-a",
        )


def test_dynamic_replanning_feature_is_default_off(
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    service = PlanningApplicationService(
        uow_factory=uow_factory,
        tenant_id="test-tenant",
        max_concurrency=4,
        feature_gates=FeatureGateSet.from_config("minimal"),
    )

    with pytest.raises(FeatureDisabled, match="dynamic_replanning"):
        service.get_snapshot(uuid4())
