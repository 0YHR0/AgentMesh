from __future__ import annotations

import pytest

from agentmesh.api.schemas import TaskResponse
from agentmesh.application.output_policies import (
    OUTPUT_POLICY_INPUT_KEY,
    normalize_output_policy,
)
from agentmesh.application.services import TaskApplicationService
from agentmesh.domain.coordination import CoordinatedPlan, SubtaskSpec, SubtaskStatus
from agentmesh.domain.errors import InvalidTaskInput
from agentmesh.domain.tasks import Task, TaskAggregate, TaskExecutionMode, TaskStatus


def _plan(*, two_ends: bool = False) -> CoordinatedPlan:
    specs = [
        SubtaskSpec.create(key="research", objective="Research", input={"role": "Researcher"}),
        SubtaskSpec.create(
            key="report",
            objective="Report",
            input={"role": "Writer"},
            depends_on=("research",),
        ),
    ]
    if two_ends:
        specs.append(SubtaskSpec.create(
            key="media", objective="Media", input={"role": "Producer"},
            depends_on=("research",),
        ))
    return CoordinatedPlan.create(tuple(specs), max_concurrency=2)


def _completed(plan: CoordinatedPlan, policy: dict | None = None) -> TaskAggregate:
    task = Task.create(
        tenant_id="test-tenant",
        objective="Produce deliverables",
        input={OUTPUT_POLICY_INPUT_KEY: policy} if policy is not None else {},
        execution_mode=TaskExecutionMode.COORDINATED,
        plan_version=plan.version,
        plan_digest=plan.digest,
        max_concurrency=plan.max_concurrency,
    )
    task.status = TaskStatus.COMPLETED
    task.output = {"summary": "Demo supervisor completed", "agent": {"kind": "deterministic-demo"}}
    subtasks, dependencies = plan.materialize(task.id)
    for subtask in subtasks:
        subtask.status = SubtaskStatus.COMPLETED
        subtask.output = {"summary": f"{subtask.key} result"}
    return TaskAggregate(task=task, subtasks=subtasks, dependencies=dependencies)


def test_auto_policy_promotes_only_terminal_deliverable_not_supervisor() -> None:
    response = TaskResponse.from_aggregate(_completed(_plan()))

    assert response.output["summary"] == "Demo supervisor completed"
    assert response.primary_deliverable is not None
    assert response.primary_deliverable.subtask_key == "report"
    assert response.primary_deliverable.output["summary"] == "report result"
    assert [item.subtask_key for item in response.deliverables] == ["report"]


def test_multiple_terminal_outputs_are_not_arbitrarily_promoted() -> None:
    response = TaskResponse.from_aggregate(_completed(_plan(two_ends=True)))

    assert response.primary_deliverable is None
    assert {item.subtask_key for item in response.deliverables} == {"report", "media"}


def test_selected_primary_and_supporting_work_are_pinned() -> None:
    plan = _plan(two_ends=True)
    policy = normalize_output_policy(
        {
            "mode": "selected",
            "primary_subtask_key": "media",
            "include_subtask_keys": ["media", "research"],
        },
        execution_mode=TaskExecutionMode.COORDINATED,
        plan=plan,
    )
    assert policy is not None
    response = TaskResponse.from_aggregate(_completed(plan, policy))

    assert response.output_policy == policy
    assert response.primary_deliverable is not None
    assert response.primary_deliverable.subtask_key == "media"
    assert [item.subtask_key for item in response.deliverables] == ["media", "research"]


def test_task_creation_persists_selected_policy_and_rejects_spoofed_input(
    task_service: TaskApplicationService,
) -> None:
    plan = _plan(two_ends=True)
    created = task_service.create_task(
        "Prepare two outputs",
        execution_mode=TaskExecutionMode.COORDINATED,
        coordinated_plan=plan,
        output_policy={"mode": "selected", "primary_subtask_key": "media"},
    )

    assert created.task.input[OUTPUT_POLICY_INPUT_KEY]["primary_subtask_key"] == "media"
    assert TaskResponse.from_aggregate(created).output_policy["mode"] == "selected"
    with pytest.raises(InvalidTaskInput, match="server-managed"):
        task_service.create_task(
            "Spoof output policy",
            execution_mode=TaskExecutionMode.COORDINATED,
            coordinated_plan=plan,
            input={
                OUTPUT_POLICY_INPUT_KEY: {
                    "mode": "selected", "primary_subtask_key": "research"
                }
            },
        )


@pytest.mark.parametrize(
    "policy",
    [
        {"mode": "selected", "primary_subtask_key": "research"},
        {"mode": "selected", "primary_subtask_key": "missing"},
        {"mode": "selected", "primary_subtask_key": "report", "include_subtask_keys": ["research"]},
        {"mode": "auto", "primary_subtask_key": "report"},
    ],
)
def test_invalid_output_selection_is_rejected(policy: dict) -> None:
    with pytest.raises(InvalidTaskInput):
        normalize_output_policy(
            policy,
            execution_mode=TaskExecutionMode.COORDINATED,
            plan=_plan(),
        )
