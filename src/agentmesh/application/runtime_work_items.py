"""Canonical, framework-neutral work-item construction.

Work items are the only input contract shared by the legacy workflow runner
and managed Runtime Assignment construction.  The builder intentionally
returns plain copied JSON values; LangGraph and provider adapter types never
enter this boundary.
"""

from __future__ import annotations

from typing import Any

from agentmesh.application.ports import WorkflowWorkItem
from agentmesh.domain.errors import InvalidTaskInput, InvalidTaskTransition
from agentmesh.domain.tasks import RunRole, Task, TaskExecutionMode, TaskRun
from agentmesh.runtime_sdk.canonical import canonical_json_bytes


class CanonicalWorkItemBuilder:
    """Build role-aware work items from immutable Task/Run projections."""

    def __init__(self, coordinated_scheduler: Any | None = None) -> None:
        self._coordinated_scheduler = coordinated_scheduler

    def build(
        self,
        task: Task,
        run: TaskRun,
        *,
        uow: Any | None = None,
    ) -> WorkflowWorkItem:
        if task.execution_mode is TaskExecutionMode.COORDINATED:
            if self._coordinated_scheduler is None or uow is None:
                raise InvalidTaskTransition(
                    "Coordinated work-item construction requires a transaction"
                )
            legacy_builder = getattr(self._coordinated_scheduler, "_work_item_input_legacy", None)
            if legacy_builder is None:
                raise InvalidTaskTransition("Coordinated scheduler has no work-item source")
            objective, input_value = legacy_builder(
                uow, task, run
            )
        elif run.role is RunRole.REVIEWER:
            objective = "Review the current candidate against the pinned acceptance contract"
            input_value = {
                "candidate_output": dict(task.candidate_output or {}),
                "acceptance_criteria": [
                    criterion.to_dict() for criterion in task.acceptance_criteria
                ],
            }
        else:
            objective = task.objective
            input_value = dict(task.input)
            if run.revision_number:
                input_value["review_context"] = {
                    "revision_number": run.revision_number,
                    "previous_candidate": dict(task.candidate_output or {}),
                    "latest_review": dict(task.latest_review or {}),
                }
        if type(objective) is not str or not objective.strip() or type(input_value) is not dict:
            raise InvalidTaskInput("Canonical Runtime work item is invalid")
        # Force a bounded canonical copy before it can be used to build an
        # Assignment.  The Runtime SDK applies the stricter secret policy.
        try:
            from agentmesh.runtime_sdk.canonical import decode_json

            encoded = canonical_json_bytes({"objective": objective, "input": input_value})
            if len(encoded) > 262_144:
                raise InvalidTaskInput("Canonical Runtime work item exceeds its byte limit")
            copied = decode_json(encoded)
        except InvalidTaskInput:
            raise
        except Exception as exc:
            raise InvalidTaskInput("Canonical Runtime work item is not JSON compatible") from exc
        return WorkflowWorkItem(
            objective=copied["objective"],
            input=copied["input"],
        )


__all__ = ["CanonicalWorkItemBuilder"]
