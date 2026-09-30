"""Projection of execution-independent acceptance and explicit human overrides."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from agentmesh.application.output_policies import project_deliverables
from agentmesh.domain.coordination import CoordinatedPlan
from agentmesh.domain.deliverable_acceptance import (
    ACCEPTANCE_POLICY_INPUT_KEY,
    evaluate_checks,
    json_digest,
    normalize_acceptance_policy,
    resolve_path,
)
from agentmesh.domain.errors import InvalidTaskInput
from agentmesh.domain.tasks import TaskAggregate, TaskExecutionMode, TaskStatus


def acceptance_work_item_context(task: Any, subtask_key: str) -> dict[str, Any] | None:
    """Expose only explicitly referenced caller facts and required result fields."""
    policy = task.input.get(ACCEPTANCE_POLICY_INPUT_KEY)
    if not isinstance(policy, dict) or policy.get("target_subtask_key") != subtask_key:
        return None
    policy = normalize_acceptance_policy(policy, target_subtask_key=subtask_key)
    evidence: dict[str, Any] = {}
    roots: set[str] = set()
    for check in policy["checks"]:
        if check["kind"] != "RATE_THRESHOLD":
            roots.add(check["path"][0])
            continue
        for name in ("numerator", "denominator", "claim"):
            reference = check.get(name)
            if reference is None:
                continue
            if reference["source"] == "DELIVERABLE":
                roots.add(reference["path"][0])
                continue
            found, value = resolve_path(task.input, reference["path"])
            if found:
                cursor = evidence
                for part in reference["path"][:-1]:
                    if not isinstance(cursor.get(part), dict):
                        cursor[part] = {}
                    cursor = cursor[part]
                cursor[reference["path"][-1]] = deepcopy(value)
    return {
        "version": 1, "checks": policy["checks"], "task_input": evidence,
        "output_roots": sorted(roots - {"agent", "execution", "memory_candidates"}),
        "instruction": (
            "Return strict JSON with a string summary and the configured output fields. "
            "task_input contains only explicitly referenced acceptance facts, not all Task input. "
            "Missing evidence is unknown; never substitute different units or invent values. "
            "A ratio claim requires value, the exact ratio unit, and the configured scale. "
            "The platform computes acceptance independently; your verdict cannot grant it."
        ),
    }


def normalize_task_acceptance_policy(
    value: dict[str, Any] | None,
    *,
    execution_mode: TaskExecutionMode,
    plan: CoordinatedPlan | None,
    output_policy: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if value is None:
        return None, output_policy
    if execution_mode is not TaskExecutionMode.COORDINATED or plan is None:
        raise InvalidTaskInput("Deliverable acceptance requires a coordinated Task")
    terminal = {spec.key for spec in plan.specs} - {
        key for spec in plan.specs for key in spec.depends_on
    }
    selected = output_policy or {}
    target = selected.get("primary_subtask_key")
    if target is None and len(terminal) == 1:
        target = next(iter(terminal))
    if target not in terminal:
        raise InvalidTaskInput("Acceptance requires an explicitly selected primary deliverable")
    policy = normalize_acceptance_policy(value, target_subtask_key=target)
    pinned = {
        "mode": "selected",
        "primary_subtask_key": target,
        "include_subtask_keys": selected.get("include_subtask_keys"),
    }
    return policy, pinned


def project_deliverable_acceptance(aggregate: TaskAggregate) -> dict[str, Any]:
    task = aggregate.task
    policy = task.input.get(ACCEPTANCE_POLICY_INPUT_KEY)
    result: dict[str, Any] = {
        "status": "NOT_CONFIGURED",
        "checks": [],
        "delivery_allowed": task.status is TaskStatus.COMPLETED,
        "policy_digest": None,
        "deliverable_digest": None,
        "evidence_digest": None,
        "target_subtask_key": None,
        "human_decision": None,
    }
    if policy is None:
        return result
    result.update(status="NOT_READY", delivery_allowed=False)
    if not isinstance(policy, dict):
        return {**result, "status": "FAILED", "reason": "invalid_pinned_policy"}
    target = policy.get("target_subtask_key")
    result["target_subtask_key"] = target
    if (
        task.execution_mode is not TaskExecutionMode.COORDINATED
        or not isinstance(target, str)
        or not target.strip()
    ):
        return {**result, "status": "FAILED", "reason": "invalid_pinned_policy"}
    try:
        normalized = normalize_acceptance_policy(policy, target_subtask_key=target)
        result["policy_digest"] = json_digest(normalized)
    except InvalidTaskInput:
        return {**result, "status": "FAILED", "reason": "invalid_pinned_policy"}
    if task.status is not TaskStatus.COMPLETED:
        return result
    _, deliverables = project_deliverables(aggregate)
    primary = next(
        (item for item in deliverables if item["primary"] and item["subtask_key"] == target), None
    )
    if primary is None:
        return {**result, "status": "FAILED", "reason": "missing_pinned_deliverable"}
    try:
        result["deliverable_digest"] = json_digest(
            {"target_subtask_key": target, "output": primary["output"]}
        )
        result["evidence_digest"] = json_digest(
            {"task_input": task.input, "policy": normalized, "output": primary["output"]}
        )
        checks = evaluate_checks(normalized, task.input, primary["output"])
    except (InvalidTaskInput, ValueError, TypeError, KeyError):
        return {**result, "status": "FAILED", "reason": "invalid_acceptance_evidence"}
    result["checks"] = checks
    required = [check for check in checks if check["required"]]
    if any(check["status"] == "FAIL" for check in required):
        result["status"] = "FAILED"
    elif (
        any(check["status"] == "UNKNOWN" for check in required)
        or normalized["require_human_review"]
    ):
        result["status"] = "NEEDS_REVIEW"
    else:
        result.update(status="PASSED", delivery_allowed=True)
    decisions = sorted(
        getattr(aggregate, "deliverable_decisions", []),
        key=lambda decision: (decision.created_at, str(decision.id)),
    )
    for decision in decisions:
        if (
            decision.task_id != task.id
            or decision.previous_status is not TaskStatus.COMPLETED
            or decision.resulting_status is not TaskStatus.COMPLETED
        ):
            continue
        action = decision.action.value
        if action not in {"ACCEPT_DELIVERABLE", "REJECT_DELIVERABLE"}:
            continue
        if any(
            decision.details.get(key) != result[key]
            for key in (
                "policy_digest",
                "deliverable_digest",
                "evidence_digest",
            )
        ):
            continue
        accepted = action == "ACCEPT_DELIVERABLE"
        result.update(
            status="HUMAN_ACCEPTED" if accepted else "HUMAN_REJECTED",
            delivery_allowed=accepted,
            human_decision={
                "id": str(decision.id),
                "decision": "ACCEPT" if accepted else "REJECT",
                "actor": decision.actor,
                "reason": decision.reason,
                "created_at": decision.created_at.isoformat(),
            },
        )
    return result
