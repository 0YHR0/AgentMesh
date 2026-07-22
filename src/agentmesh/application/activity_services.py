from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.domain.errors import TaskNotFound


@dataclass(frozen=True)
class ActivityEvent:
    id: str
    category: str
    action: str
    status: str
    title: str
    occurred_at: datetime
    entity_type: str
    entity_id: str
    actor: str | None = None
    trace_id: str | None = None
    details: dict[str, Any] | None = None


class TaskActivityService:
    """Project durable Task-adjacent ledgers into a redacted operator timeline."""

    def __init__(self, *, uow_factory: UnitOfWorkFactory, tenant_id: str) -> None:
        self._uow_factory = uow_factory
        self._tenant_id = tenant_id

    def timeline(self, task_id: UUID, *, limit: int) -> list[ActivityEvent]:
        with self._uow_factory() as uow:
            task = uow.tasks.get(task_id)
            if task is None or task.tenant_id != self._tenant_id:
                raise TaskNotFound(str(task_id))
            runs = uow.runs.list_for_task(task_id)
            attempts = uow.attempts.list_for_task(task_id)
            subtasks = uow.subtasks.list_for_task(task_id)
            handoffs = uow.handoffs.list_for_task(task_id)
            patches = uow.plan_patches.list_for_task(task_id)
            resolutions = uow.task_resolutions.list_for_task(task_id)
            invocations = uow.tool_invocations.list_for_task(task_id)
            correlation = uow.remote_correlations.get_for_task(task_id)
            versions = uow.artifact_versions.list_for_producer_runs([run.id for run in runs])

        events = [
            self._event(
                "task",
                task.id,
                "created",
                "CREATED",
                "Task created",
                task.created_at,
                details={
                    "execution_mode": task.execution_mode.value,
                    "project_id": task.project_id,
                },
            )
        ]
        if task.updated_at > task.created_at:
            events.append(
                self._event(
                    "task",
                    task.id,
                    "state",
                    task.status.value,
                    "Task state updated",
                    task.updated_at,
                    details={"version": task.version},
                )
            )
        for run in runs:
            safe = {"role": run.role.value, "agent_id": run.agent_id}
            events.append(
                self._event(
                    "run", run.id, "queued", "QUEUED", "Run queued", run.queued_at, details=safe
                )
            )
            if run.started_at:
                events.append(
                    self._event(
                        "run",
                        run.id,
                        "started",
                        "RUNNING",
                        "Run started",
                        run.started_at,
                        details=safe,
                    )
                )
            if run.completed_at:
                events.append(
                    self._event(
                        "run",
                        run.id,
                        "completed",
                        run.status.value,
                        "Run completed",
                        run.completed_at,
                        details=safe,
                    )
                )
        for attempt in attempts:
            safe = {
                "run_id": str(attempt.run_id),
                "worker_id": attempt.worker_id,
                "fencing_token": attempt.fencing_token,
            }
            events.append(
                self._event(
                    "attempt",
                    attempt.id,
                    "started",
                    "RUNNING",
                    "Attempt leased",
                    attempt.started_at,
                    actor=attempt.worker_id,
                    trace_id=attempt.trace_id,
                    details=safe,
                )
            )
            if attempt.completed_at:
                events.append(
                    self._event(
                        "attempt",
                        attempt.id,
                        "completed",
                        attempt.status.value,
                        "Attempt completed",
                        attempt.completed_at,
                        actor=attempt.worker_id,
                        trace_id=attempt.trace_id,
                        details=safe,
                    )
                )
        for subtask in subtasks:
            events.append(
                self._event(
                    "subtask",
                    subtask.id,
                    "created",
                    "CREATED",
                    f"Subtask {subtask.key} created",
                    subtask.created_at,
                )
            )
            if subtask.updated_at > subtask.created_at:
                events.append(
                    self._event(
                        "subtask",
                        subtask.id,
                        "state",
                        subtask.status.value,
                        f"Subtask {subtask.key} updated",
                        subtask.updated_at,
                    )
                )
        for patch in patches:
            events.append(
                self._event(
                    "plan",
                    patch.id,
                    "verified",
                    "VERIFIED",
                    "Plan Patch verified",
                    patch.created_at,
                    actor=patch.requested_by,
                    details={"proposed_plan_version": patch.proposed_plan_version},
                )
            )
            if patch.applied_at:
                events.append(
                    self._event(
                        "plan",
                        patch.id,
                        "applied",
                        "APPLIED",
                        "Plan Patch applied",
                        patch.applied_at,
                        actor=patch.requested_by,
                        details={"proposed_plan_version": patch.proposed_plan_version},
                    )
                )
        for handoff in handoffs:
            events.append(
                self._event(
                    "handoff",
                    handoff.id,
                    "requested",
                    "REQUESTED",
                    "Handoff requested",
                    handoff.requested_at,
                    actor=handoff.requested_by,
                    trace_id=handoff.source_trace_id,
                    details={
                        "source_agent_id": handoff.source_agent_id,
                        "target_agent_id": handoff.target_agent_id,
                    },
                )
            )
            if handoff.decided_at:
                events.append(
                    self._event(
                        "handoff",
                        handoff.id,
                        "decided",
                        handoff.status.value,
                        "Handoff decided",
                        handoff.decided_at,
                        actor=handoff.decided_by,
                        trace_id=handoff.source_trace_id,
                    )
                )
        for invocation in invocations:
            safe = {
                "run_id": str(invocation.run_id),
                "tool_key": invocation.tool_key,
                "server_name": invocation.server_name,
                "side_effect": invocation.side_effect.value,
            }
            events.append(
                self._event(
                    "tool",
                    invocation.id,
                    "started",
                    "RUNNING",
                    "MCP Tool invocation started",
                    invocation.started_at,
                    details=safe,
                )
            )
            if invocation.completed_at:
                events.append(
                    self._event(
                        "tool",
                        invocation.id,
                        "completed",
                        invocation.status.value,
                        "MCP Tool invocation completed",
                        invocation.completed_at,
                        details=safe,
                    )
                )
        for version in versions:
            events.append(
                self._event(
                    "artifact",
                    version.id,
                    "available",
                    version.status.value,
                    f"Artifact Version {version.version_number} available",
                    version.created_at,
                    details={
                        "artifact_id": str(version.artifact_id),
                        "producer_run_id": str(version.producer_run_id),
                        "media_type": version.media_type,
                        "size_bytes": version.size_bytes,
                        "sha256": version.sha256,
                    },
                )
            )
        for resolution in resolutions:
            events.append(
                self._event(
                    "resolution",
                    resolution.id,
                    "recorded",
                    resolution.resulting_status.value,
                    f"Resolution: {resolution.action.value}",
                    resolution.created_at,
                    actor=resolution.actor,
                    details={
                        "previous_status": resolution.previous_status.value,
                        "resulting_status": resolution.resulting_status.value,
                    },
                )
            )
        if correlation is not None:
            safe = {
                "run_id": str(correlation.run_id),
                "peer_id": str(correlation.peer_id),
                "protocol_version": correlation.protocol_version,
                "remote_task_bound": correlation.remote_task_id is not None,
            }
            events.append(
                self._event(
                    "a2a",
                    correlation.id,
                    "created",
                    "PREPARED",
                    "A2A delegation prepared",
                    correlation.created_at,
                    details=safe,
                )
            )
            if correlation.updated_at > correlation.created_at:
                events.append(
                    self._event(
                        "a2a",
                        correlation.id,
                        "state",
                        correlation.status.value,
                        "A2A delegation updated",
                        correlation.updated_at,
                        details=safe,
                    )
                )

        events.sort(key=lambda item: (item.occurred_at, item.id), reverse=True)
        return events[:limit]

    @staticmethod
    def _event(
        category: str,
        entity_id: UUID,
        action: str,
        status: str,
        title: str,
        occurred_at: datetime,
        *,
        actor: str | None = None,
        trace_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> ActivityEvent:
        return ActivityEvent(
            id=f"{category}:{entity_id}:{action}",
            category=category,
            action=action,
            status=status,
            title=title,
            occurred_at=occurred_at,
            entity_type=category,
            entity_id=str(entity_id),
            actor=actor,
            trace_id=trace_id,
            details=details,
        )
