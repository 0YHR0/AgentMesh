"""Pinned Task deliverable selection, separate from execution/supervisor output."""

from __future__ import annotations

from typing import Any

from agentmesh.domain.coordination import CoordinatedPlan, SubtaskStatus
from agentmesh.domain.errors import InvalidTaskInput
from agentmesh.domain.tasks import TaskAggregate, TaskExecutionMode, TaskStatus

OUTPUT_POLICY_INPUT_KEY = "agentmesh_output_policy"


def normalize_output_policy(
    value: dict[str, Any] | None,
    *,
    execution_mode: TaskExecutionMode,
    plan: CoordinatedPlan | None,
) -> dict[str, Any] | None:
    """Validate a user choice against the immutable initial Subtask plan."""
    if value is None:
        return None
    if execution_mode is not TaskExecutionMode.COORDINATED or plan is None:
        raise InvalidTaskInput("Output policy is only valid for coordinated Tasks")
    mode = value.get("mode", "auto")
    if mode not in {"auto", "selected"}:
        raise InvalidTaskInput("Output policy mode must be auto or selected")
    primary = value.get("primary_subtask_key")
    included = value.get("include_subtask_keys")
    keys = {spec.key for spec in plan.specs}
    terminal = keys - {key for spec in plan.specs for key in spec.depends_on}
    if mode == "auto":
        if primary is not None or included is not None:
            raise InvalidTaskInput("Automatic output policy cannot select Subtasks")
        return {"mode": "auto", "primary_subtask_key": None, "include_subtask_keys": None}
    if not isinstance(primary, str) or primary not in terminal:
        raise InvalidTaskInput("Primary deliverable must be a terminal Subtask")
    if included is not None:
        if not isinstance(included, list) or any(
            not isinstance(key, str) or key not in keys for key in included
        ):
            raise InvalidTaskInput("Included deliverables must reference known Subtasks")
        if len(included) != len(set(included)):
            raise InvalidTaskInput("Included deliverables must be unique")
        if primary not in included:
            raise InvalidTaskInput("Included deliverables must contain the primary Subtask")
    return {
        "mode": "selected",
        "primary_subtask_key": primary,
        "include_subtask_keys": list(included) if included is not None else None,
    }


def project_deliverables(
    aggregate: TaskAggregate,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Expose business output without replacing the supervisor's audit record."""
    task = aggregate.task
    raw_policy = task.input.get(OUTPUT_POLICY_INPUT_KEY)
    policy = raw_policy if isinstance(raw_policy, dict) else {"mode": "auto"}
    if task.execution_mode is not TaskExecutionMode.COORDINATED:
        if task.status is TaskStatus.COMPLETED and task.output is not None:
            return policy, [{
                "source": "task",
                "subtask_key": None,
                "label": "Task result",
                "output": dict(task.output),
                "primary": True,
            }]
        return policy, []
    if task.status is not TaskStatus.COMPLETED:
        return policy, []
    by_key = {subtask.key: subtask for subtask in aggregate.subtasks}
    predecessor_ids = {edge.predecessor_id for edge in aggregate.dependencies}
    terminal = {
        subtask.key for subtask in aggregate.subtasks if subtask.id not in predecessor_ids
    }
    primary_key = policy.get("primary_subtask_key") if policy.get("mode") == "selected" else None
    included = policy.get("include_subtask_keys")
    if isinstance(included, list):
        selected_keys = [key for key in included if key in by_key]
    else:
        selected_keys = sorted(terminal)
        if primary_key in by_key and primary_key not in selected_keys:
            selected_keys.insert(0, primary_key)
    if primary_key is None and len(selected_keys) == 1:
        primary_key = selected_keys[0]
    if primary_key in selected_keys:
        selected_keys = [primary_key] + [key for key in selected_keys if key != primary_key]
    deliverables: list[dict[str, Any]] = []
    for key in selected_keys:
        subtask = by_key.get(key)
        if (
            subtask is None
            or subtask.status is not SubtaskStatus.COMPLETED
            or subtask.output is None
        ):
            continue
        deliverables.append({
            "source": "subtask",
            "subtask_key": key,
            "label": str(subtask.input.get("role") or key),
            "output": dict(subtask.output),
            "primary": key == primary_key,
        })
    return policy, deliverables
