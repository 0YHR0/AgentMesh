"""Acceptance projections stay independent of execution and supporting artifacts."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from agentmesh.application.deliverable_acceptance import (
    normalize_task_acceptance_policy,
    project_deliverable_acceptance,
)
from agentmesh.application.output_policies import OUTPUT_POLICY_INPUT_KEY
from agentmesh.domain.coordination import CoordinatedPlan, SubtaskSpec, SubtaskStatus
from agentmesh.domain.deliverable_acceptance import ACCEPTANCE_POLICY_INPUT_KEY
from agentmesh.domain.errors import InvalidTaskInput
from agentmesh.domain.resolutions import TaskResolution, TaskResolutionAction
from agentmesh.domain.tasks import Task, TaskAggregate, TaskExecutionMode, TaskStatus


def _plan(*, multiple: bool = False) -> CoordinatedPlan:
    specs = [
        SubtaskSpec.create(key="research", objective="Research"),
        SubtaskSpec.create(key="main", objective="Main report", depends_on=("research",)),
    ]
    if multiple:
        specs.append(SubtaskSpec.create(
            key="attachment", objective="Supporting attachment", depends_on=("research",),
        ))
    return CoordinatedPlan.create(tuple(specs), max_concurrency=2)


def _policy(*, human: bool = False, equals: bool = False) -> dict:
    check = {
        "key": "summary", "description": "Main output summary", "required": True,
        "kind": "OUTPUT_PATH_EQUALS" if equals else "OUTPUT_PATH_EXISTS", "path": ["summary"],
    }
    if equals:
        check["expected"] = "Approved summary"
    return {"require_human_review": human, "checks": [check]}


def _aggregate(*, policy: dict | None = None, output: dict | None = None) -> TaskAggregate:
    plan = _plan(multiple=True)
    normalized, selection = normalize_task_acceptance_policy(
        policy, execution_mode=TaskExecutionMode.COORDINATED, plan=plan,
        output_policy={"mode": "selected", "primary_subtask_key": "main"},
    )
    input_data = {OUTPUT_POLICY_INPUT_KEY: selection}
    if normalized is not None:
        input_data[ACCEPTANCE_POLICY_INPUT_KEY] = normalized
    task = Task.create(
        tenant_id="test-tenant", objective="Deliver main output", input=input_data,
        execution_mode=TaskExecutionMode.COORDINATED,
        plan_version=plan.version, plan_digest=plan.digest, max_concurrency=plan.max_concurrency,
    )
    task.status = TaskStatus.COMPLETED
    task.output = {"summary": "Supervisor audit, not a deliverable"}
    subtasks, dependencies = plan.materialize(task.id)
    for subtask in subtasks:
        subtask.status = SubtaskStatus.COMPLETED
        subtask.output = output if subtask.key == "main" and output is not None else {
            "summary": f"{subtask.key} output",
        }
    return TaskAggregate(task=task, subtasks=subtasks, dependencies=dependencies)


def _decision(aggregate: TaskAggregate, *, accept: bool, at: datetime) -> TaskResolution:
    projection = project_deliverable_acceptance(aggregate)
    return TaskResolution.create(
        task_id=aggregate.task.id,
        action=(TaskResolutionAction.ACCEPT_DELIVERABLE if accept
                else TaskResolutionAction.REJECT_DELIVERABLE),
        actor="authorized-reviewer", reason="Reviewed the pinned main output",
        previous_status=TaskStatus.COMPLETED, resulting_status=TaskStatus.COMPLETED,
        previous_error=None,
        details={key: projection[key] for key in (
            "policy_digest", "deliverable_digest", "evidence_digest",
        )}, at=at,
    )


def test_normalization_pins_unique_terminal_and_preserves_supporting_selection() -> None:
    policy, output_policy = normalize_task_acceptance_policy(
        _policy(), execution_mode=TaskExecutionMode.COORDINATED, plan=_plan(),
        output_policy={"mode": "auto"},
    )
    assert policy["target_subtask_key"] == "main"
    assert output_policy == {
        "mode": "selected", "primary_subtask_key": "main", "include_subtask_keys": None,
    }
    selected = {
        "mode": "selected", "primary_subtask_key": "attachment",
        "include_subtask_keys": ["attachment", "research"],
    }
    policy, output_policy = normalize_task_acceptance_policy(
        _policy(), execution_mode=TaskExecutionMode.COORDINATED,
        plan=_plan(multiple=True), output_policy=selected,
    )
    assert policy["target_subtask_key"] == "attachment"
    assert output_policy == selected


@pytest.mark.parametrize("primary", [None, "research", "missing"])
def test_multiple_terminal_acceptance_requires_valid_primary(primary: str | None) -> None:
    with pytest.raises(InvalidTaskInput):
        normalize_task_acceptance_policy(
            _policy(), execution_mode=TaskExecutionMode.COORDINATED,
            plan=_plan(multiple=True), output_policy={"primary_subtask_key": primary},
        )


def test_acceptance_is_optional_and_coordinated_only() -> None:
    existing = {"mode": "auto"}
    assert normalize_task_acceptance_policy(
        None, execution_mode=TaskExecutionMode.DIRECT, plan=None, output_policy=existing,
    ) == (None, existing)
    for mode in (TaskExecutionMode.DIRECT, TaskExecutionMode.REVIEWED):
        with pytest.raises(InvalidTaskInput, match="coordinated"):
            normalize_task_acceptance_policy(
                _policy(), execution_mode=mode, plan=_plan(), output_policy=None,
            )
    with pytest.raises(InvalidTaskInput):
        normalize_task_acceptance_policy(
            {**_policy(), "target_subtask_key": "attachment"},
            execution_mode=TaskExecutionMode.COORDINATED, plan=_plan(), output_policy=None,
        )


def test_projection_legacy_and_unfinished_states_do_not_relabel_execution() -> None:
    legacy = _aggregate()
    assert project_deliverable_acceptance(legacy)["status"] == "NOT_CONFIGURED"
    assert project_deliverable_acceptance(legacy)["delivery_allowed"] is True
    aggregate = _aggregate(policy=_policy())
    aggregate.task.status = TaskStatus.RUNNING
    projection = project_deliverable_acceptance(aggregate)
    assert projection["status"] == "NOT_READY"
    assert projection["delivery_allowed"] is False
    assert projection["checks"] == []
    assert projection["policy_digest"]
    assert projection["deliverable_digest"] is None
    assert aggregate.task.status is TaskStatus.RUNNING


@pytest.mark.parametrize(
    ("policy", "output", "expected", "check_status"),
    [
        (_policy(), {"summary": "Main report"}, "PASSED", "PASS"),
        (_policy(human=True), {"summary": "Main report"}, "NEEDS_REVIEW", "PASS"),
        (_policy(), {"acceptance_status": "PASS"}, "NEEDS_REVIEW", "UNKNOWN"),
        (_policy(equals=True), {"summary": "Not approved"}, "FAILED", "FAIL"),
    ],
)
def test_projection_statuses_ignore_self_declared_model_verdicts(
    policy: dict, output: dict, expected: str, check_status: str,
) -> None:
    aggregate = _aggregate(policy=policy, output=output)
    projection = project_deliverable_acceptance(aggregate)
    assert projection["status"] == expected
    assert projection["delivery_allowed"] is (expected == "PASSED")
    assert projection["checks"][0]["status"] == check_status
    assert aggregate.task.status is TaskStatus.COMPLETED
    assert projection["target_subtask_key"] == "main"


def test_human_override_preserves_failed_checks_output_and_execution() -> None:
    aggregate = _aggregate(policy=_policy(equals=True), output={"summary": "Not approved"})
    before = project_deliverable_acceptance(aggregate)
    output_before = deepcopy(aggregate.subtasks[1].output)
    at = datetime(2026, 9, 30, tzinfo=timezone.utc)
    accepted = _decision(aggregate, accept=True, at=at)
    aggregate.deliverable_decisions.append(accepted)
    projection = project_deliverable_acceptance(aggregate)
    assert projection["status"] == "HUMAN_ACCEPTED"
    assert projection["delivery_allowed"] is True
    assert projection["checks"] == before["checks"]
    assert aggregate.subtasks[1].output == output_before
    assert aggregate.task.status is TaskStatus.COMPLETED
    assert projection["human_decision"]["actor"] == "authorized-reviewer"
    rejected = _decision(aggregate, accept=False, at=at + timedelta(seconds=1))
    aggregate.deliverable_decisions.insert(0, rejected)
    projection = project_deliverable_acceptance(aggregate)
    assert projection["status"] == "HUMAN_REJECTED"
    assert projection["delivery_allowed"] is False
    assert projection["checks"] == before["checks"]
    assert projection["human_decision"]["id"] == str(rejected.id)


@pytest.mark.parametrize("digest", ["policy_digest", "deliverable_digest", "evidence_digest"])
def test_stale_human_decision_never_overrides_current_evidence(digest: str) -> None:
    aggregate = _aggregate(policy=_policy(equals=True))
    decision = _decision(aggregate, accept=True, at=datetime.now(timezone.utc))
    decision.details[digest] = "stale"
    aggregate.deliverable_decisions.append(decision)
    projection = project_deliverable_acceptance(aggregate)
    assert projection["status"] == "FAILED"
    assert projection["human_decision"] is None
    assert projection["delivery_allowed"] is False


def test_changed_input_invalidates_prior_override_even_if_main_output_is_unchanged() -> None:
    aggregate = _aggregate(policy=_policy(equals=True))
    aggregate.deliverable_decisions.append(
        _decision(aggregate, accept=True, at=datetime.now(timezone.utc)),
    )
    previous = project_deliverable_acceptance(aggregate)
    aggregate.task.input["new_fact"] = {"value": 10, "unit": "buyer"}
    current = project_deliverable_acceptance(aggregate)
    assert current["deliverable_digest"] == previous["deliverable_digest"]
    assert current["evidence_digest"] != previous["evidence_digest"]
    assert current["status"] == "FAILED"
    assert current["human_decision"] is None


def test_missing_selected_output_blocks_delivery_and_attachments_are_not_validated() -> None:
    aggregate = _aggregate(policy=_policy())
    supporting = next(unit for unit in aggregate.subtasks if unit.key == "attachment")
    supporting.output = {"attachment": "unverified", "summary": None, "claims": "PASS"}
    projection = project_deliverable_acceptance(aggregate)
    assert projection["status"] == "PASSED"
    assert len(projection["checks"]) == 1
    assert projection["checks"][0]["evidence"]["path"] == ["summary"]
    main = next(unit for unit in aggregate.subtasks if unit.key == "main")
    main.output = None
    projection = project_deliverable_acceptance(aggregate)
    assert projection["status"] == "FAILED"
    assert projection["reason"] == "missing_pinned_deliverable"
    assert projection["delivery_allowed"] is False


def test_optional_failed_checks_do_not_block_required_success() -> None:
    policy = _policy()
    policy["checks"].append({
        "key": "optional", "description": "Optional equality", "required": False,
        "kind": "OUTPUT_PATH_EQUALS", "path": ["optional"], "expected": "wanted",
    })
    projection = project_deliverable_acceptance(_aggregate(
        policy=policy, output={"summary": "Main report", "optional": "different"},
    ))
    assert projection["status"] == "PASSED"
    assert [check["status"] for check in projection["checks"]] == ["PASS", "FAIL"]
