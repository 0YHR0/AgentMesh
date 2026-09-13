from uuid import uuid4

from agentmesh.application.runtime_work_items import CanonicalWorkItemBuilder
from agentmesh.domain.tasks import (
    AcceptanceCriterion,
    AcceptanceCriterionKind,
    RunRole,
    Task,
    TaskExecutionMode,
    TaskRun,
)


def _run(task, *, role=RunRole.EXECUTOR, revision=0):
    return TaskRun.request(
        task.id,
        "demo-agent",
        agent_version_id=uuid4(),
        agent_version_digest="a" * 64,
        role=role,
        revision_number=revision,
    )


def test_builder_uses_revision_context() -> None:
    task = Task.create(
        tenant_id="tenant-a", objective="Draft a release note", input={"source": "brief"}
    )
    run = _run(task, revision=2)
    item = CanonicalWorkItemBuilder().build(task, run)
    assert item.objective == task.objective
    assert item.input["review_context"]["revision_number"] == 2


def test_builder_copies_direct_input() -> None:
    task = Task.create(tenant_id="tenant-a", objective="Do work", input={"nested": {"x": 1}})
    run = _run(task)
    item = CanonicalWorkItemBuilder().build(task, run)
    task.input["nested"]["x"] = 9
    assert item.input == {"nested": {"x": 1}}


def test_builder_uses_review_role_contract() -> None:
    criterion = AcceptanceCriterion.create(
        key="quality",
        description="Quality is acceptable",
        kind=AcceptanceCriterionKind.OUTPUT_PATH_EXISTS,
        path=("summary",),
    )
    task = Task.create(
        tenant_id="tenant-a",
        objective="Draft a release note",
        input={"source": "brief"},
        execution_mode=TaskExecutionMode.REVIEWED,
        acceptance_criteria=(criterion,),
    )
    task.candidate_output = {"summary": "candidate"}
    run = _run(task, role=RunRole.REVIEWER)
    item = CanonicalWorkItemBuilder().build(task, run)
    assert item.objective == "Review the current candidate against the pinned acceptance contract"
    assert item.input == {
        "candidate_output": {"summary": "candidate"},
        "acceptance_criteria": [criterion.to_dict()],
    }
