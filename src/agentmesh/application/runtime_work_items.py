"""Canonical, framework-neutral work-item construction.

Work items are the only input contract shared by the legacy workflow runner
and managed Runtime Assignment construction.  The builder intentionally
returns plain copied JSON values; LangGraph and provider adapter types never
enter this boundary.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from agentmesh.application.deliverable_acceptance import acceptance_work_item_context
from agentmesh.application.ports import WorkflowWorkItem
from agentmesh.domain.deliverable_acceptance import ACCEPTANCE_POLICY_INPUT_KEY
from agentmesh.domain.errors import InvalidTaskInput, InvalidTaskTransition
from agentmesh.domain.handoffs import HandoffStatus
from agentmesh.domain.tasks import RunRole, Task, TaskExecutionMode, TaskRun
from agentmesh.runtime_sdk.canonical import canonical_json_bytes, decode_json


def work_item_from_snapshot(run: TaskRun) -> WorkflowWorkItem | None:
    """Return the input actually pinned for a coordinated Run, if one exists."""
    snapshot = run.work_item_snapshot
    if snapshot is None:
        return None
    item = snapshot.get("work_item")
    if snapshot.get("schema_version") != 1 or not isinstance(item, dict):
        raise InvalidTaskTransition("Run work-item snapshot is invalid")
    objective, input_value = item.get("objective"), item.get("input")
    if not isinstance(objective, str) or not objective or not isinstance(input_value, dict):
        raise InvalidTaskTransition("Run work-item snapshot is invalid")
    return WorkflowWorkItem(
        objective=objective, input=decode_json(canonical_json_bytes(input_value))
    )


def build_work_item_snapshot(
    uow: Any, task: Task, run: TaskRun, work_item: WorkflowWorkItem
) -> dict[str, Any]:
    """Capture exact coordinator input plus evidence for each delivered edge.

    This is the pre-memory canonical work item, not a claim about an LLM's full
    prompt or a free-form Agent conversation. The Task reader may inspect the
    payload; the redacted interaction projection never includes it.
    """
    transfers: list[dict[str, Any]] = []
    if run.role is RunRole.EXECUTOR and run.subtask_id is not None:
        subtasks = {item.id: item for item in uow.subtasks.list_for_task(task.id)}
        target = subtasks.get(run.subtask_id)
        if target is None:
            raise InvalidTaskTransition("Run work-item target Subtask is missing")
        dependency_outputs = work_item.input.get("dependency_outputs", {})
        if not isinstance(dependency_outputs, dict):
            raise InvalidTaskTransition("Run dependency outputs are invalid")
        for edge in uow.subtask_dependencies.list_for_task(task.id):
            if edge.successor_id != target.id:
                continue
            source = subtasks.get(edge.predecessor_id)
            if source is None or source.key not in dependency_outputs:
                raise InvalidTaskTransition("Run dependency output is missing")
            payload = dependency_outputs[source.key]
            source_run = uow.runs.get(source.current_run_id) if source.current_run_id else None
            transfers.append(
                {
                    "kind": "DEPENDENCY_RESULT",
                    "source_subtask_id": str(source.id),
                    "source_key": source.key,
                    "source_run_id": str(source_run.id) if source_run else None,
                    "source_agent_id": (
                        source_run.agent_id if source_run else source.preferred_agent_id
                    ),
                    "target_subtask_id": str(target.id),
                    "target_key": target.key,
                    "target_run_id": str(run.id),
                    "target_agent_id": run.agent_id,
                    "payload": payload,
                    "payload_sha256": sha256(canonical_json_bytes(payload)).hexdigest(),
                }
            )
        accepted = work_item.input.get("accepted_handoffs", [])
        if not isinstance(accepted, list):
            raise InvalidTaskTransition("Run accepted handoffs are invalid")
        for handoff in uow.handoffs.list_for_target(
            target.id, status=HandoffStatus.ACCEPTED
        ):
            payload = handoff.execution_context()
            if payload not in accepted:
                raise InvalidTaskTransition("Run accepted handoff context is missing")
            source = subtasks.get(handoff.source_subtask_id)
            transfers.append(
                {
                    "kind": "ACCEPTED_HANDOFF",
                    "handoff_id": str(handoff.id),
                    "source_subtask_id": str(handoff.source_subtask_id),
                    "source_key": source.key if source else handoff.source_agent_id,
                    "source_run_id": str(handoff.source_run_id),
                    "source_agent_id": handoff.source_agent_id,
                    "target_subtask_id": str(target.id),
                    "target_key": target.key,
                    "target_run_id": str(run.id),
                    "target_agent_id": run.agent_id,
                    "payload": payload,
                    "payload_sha256": sha256(canonical_json_bytes(payload)).hexdigest(),
                }
            )
    return decode_json(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "work_item": {"objective": work_item.objective, "input": work_item.input},
                "transfers": transfers,
            }
        )
    )


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
            if run.subtask_id is not None and ACCEPTANCE_POLICY_INPUT_KEY in task.input:
                target = next((subtask for subtask in uow.subtasks.list_for_task(task.id)
                               if subtask.id == run.subtask_id), None)
                if target is not None:
                    contract = acceptance_work_item_context(task, target.key)
                    if contract is not None:
                        input_value["agentmesh_deliverable_contract"] = contract
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


__all__ = [
    "CanonicalWorkItemBuilder",
    "build_work_item_snapshot",
    "work_item_from_snapshot",
]
