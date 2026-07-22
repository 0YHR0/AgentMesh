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


@dataclass(frozen=True)
class InteractionEndpoint:
    type: str
    id: str
    label: str | None = None


@dataclass(frozen=True)
class InteractionEvent:
    id: str
    occurred_at: datetime
    kind: str
    source: InteractionEndpoint
    target: InteractionEndpoint
    transport: str
    payload_kind: str
    status: str
    trace_id: str | None = None
    summary: dict[str, Any] | None = None


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

    def interactions(self, task_id: UUID, *, limit: int) -> list[InteractionEvent]:
        """Project governed cross-boundary activity without exposing message payloads."""
        with self._uow_factory() as uow:
            task = uow.tasks.get(task_id)
            if task is None or task.tenant_id != self._tenant_id:
                raise TaskNotFound(str(task_id))
            runs = uow.runs.list_for_task(task_id)
            subtasks = uow.subtasks.list_for_task(task_id)
            handoffs = uow.handoffs.list_for_task(task_id)
            patches = uow.plan_patches.list_for_task(task_id)
            invocations = uow.tool_invocations.list_for_task(task_id)
            correlation = uow.remote_correlations.get_for_task(task_id)
            governed_actions = uow.policy.list_actions_for_resource(
                tenant_id=self._tenant_id,
                resource_type="task",
                resource_id=task_id,
            )

        task_endpoint = InteractionEndpoint("TASK", str(task.id), "AgentMesh HQ")
        subtask_by_id = {subtask.id: subtask for subtask in subtasks}
        run_by_id = {run.id: run for run in runs}

        def execution_source(run_id: UUID) -> InteractionEndpoint:
            run = run_by_id.get(run_id)
            subtask = subtask_by_id.get(run.subtask_id) if run and run.subtask_id else None
            if subtask is None:
                return task_endpoint
            return InteractionEndpoint("SUBTASK", str(subtask.id), subtask.key)

        events: list[InteractionEvent] = []
        for handoff in handoffs:
            source = InteractionEndpoint(
                "SUBTASK",
                str(handoff.source_subtask_id),
                subtask_by_id.get(handoff.source_subtask_id).key
                if handoff.source_subtask_id in subtask_by_id
                else handoff.source_agent_id,
            )
            target = InteractionEndpoint(
                "SUBTASK",
                str(handoff.target_subtask_id),
                subtask_by_id.get(handoff.target_subtask_id).key
                if handoff.target_subtask_id in subtask_by_id
                else handoff.target_agent_id,
            )
            safe = {
                "handoff_id": str(handoff.id),
                "source_agent_id": handoff.source_agent_id,
                "target_agent_id": handoff.target_agent_id,
            }
            events.append(
                InteractionEvent(
                    id=f"handoff:{handoff.id}:requested",
                    occurred_at=handoff.requested_at,
                    kind="HANDOFF_REQUESTED",
                    source=source,
                    target=target,
                    transport="HANDOFF",
                    payload_kind="CONTEXT_CONTRACT",
                    status="REQUESTED",
                    trace_id=handoff.source_trace_id,
                    summary=safe,
                )
            )
            if handoff.decided_at:
                events.append(
                    InteractionEvent(
                        id=f"handoff:{handoff.id}:decided",
                        occurred_at=handoff.decided_at,
                        kind=f"HANDOFF_{handoff.status.value}",
                        source=source,
                        target=target,
                        transport="HANDOFF",
                        payload_kind="CONTEXT_CONTRACT",
                        status=handoff.status.value,
                        trace_id=handoff.source_trace_id,
                        summary=safe,
                    )
                )

        for invocation in invocations:
            source = execution_source(invocation.run_id)
            target = InteractionEndpoint(
                "TOOL", f"{invocation.server_name}:{invocation.tool_key}", invocation.tool_key
            )
            safe = {
                "invocation_id": str(invocation.id),
                "run_id": str(invocation.run_id),
                "server_name": invocation.server_name,
                "tool_key": invocation.tool_key,
                "side_effect": invocation.side_effect.value,
            }
            events.append(
                InteractionEvent(
                    id=f"tool:{invocation.id}:started",
                    occurred_at=invocation.started_at,
                    kind="MCP_TOOL_STARTED",
                    source=source,
                    target=target,
                    transport="MCP",
                    payload_kind="TOOL_INVOCATION",
                    status="RUNNING",
                    summary=safe,
                )
            )
            if invocation.completed_at:
                events.append(
                    InteractionEvent(
                        id=f"tool:{invocation.id}:completed",
                        occurred_at=invocation.completed_at,
                        kind="MCP_TOOL_COMPLETED",
                        source=target,
                        target=source,
                        transport="MCP",
                        payload_kind="TOOL_RESULT_ENVELOPE",
                        status=invocation.status.value,
                        summary=safe,
                    )
                )

        if correlation is not None:
            source = execution_source(correlation.run_id)
            target = InteractionEndpoint("PEER", str(correlation.peer_id), "A2A peer")
            safe = {
                "correlation_id": str(correlation.id),
                "run_id": str(correlation.run_id),
                "peer_id": str(correlation.peer_id),
                "protocol_version": correlation.protocol_version,
                "remote_task_bound": correlation.remote_task_id is not None,
            }
            events.append(
                InteractionEvent(
                    id=f"a2a:{correlation.id}:prepared",
                    occurred_at=correlation.created_at,
                    kind="A2A_DELEGATION_PREPARED",
                    source=source,
                    target=target,
                    transport="A2A",
                    payload_kind="REMOTE_TASK",
                    status="PREPARED",
                    summary=safe,
                )
            )
            if correlation.updated_at > correlation.created_at:
                events.append(
                    InteractionEvent(
                        id=f"a2a:{correlation.id}:state",
                        occurred_at=correlation.updated_at,
                        kind="A2A_DELEGATION_STATE",
                        source=target,
                        target=source,
                        transport="A2A",
                        payload_kind="REMOTE_TASK_STATE",
                        status=correlation.status.value,
                        summary=safe,
                    )
                )

        for patch in patches:
            target = InteractionEndpoint(
                "PLAN_PATCH", str(patch.id), f"Plan v{patch.proposed_plan_version}"
            )
            safe = {"patch_id": str(patch.id), "proposed_plan_version": patch.proposed_plan_version}
            events.append(
                InteractionEvent(
                    id=f"plan:{patch.id}:verified",
                    occurred_at=patch.created_at,
                    kind="PLAN_PATCH_VERIFIED",
                    source=task_endpoint,
                    target=target,
                    transport="PLAN_PATCH",
                    payload_kind="PLAN_REPLACEMENT",
                    status="VERIFIED",
                    summary=safe,
                )
            )
            if patch.applied_at:
                events.append(
                    InteractionEvent(
                        id=f"plan:{patch.id}:applied",
                        occurred_at=patch.applied_at,
                        kind="PLAN_PATCH_APPLIED",
                        source=target,
                        target=task_endpoint,
                        transport="PLAN_PATCH",
                        payload_kind="PLAN_REPLACEMENT",
                        status="APPLIED",
                        summary=safe,
                    )
                )

        for action in governed_actions:
            target = InteractionEndpoint(
                "APPROVAL", str(action.approval_id or action.id), action.action_type.value
            )
            safe = {
                "governed_action_id": str(action.id),
                "action_type": action.action_type.value,
                "policy_result": action.policy_result.value,
            }
            events.append(
                InteractionEvent(
                    id=f"approval:{action.id}:requested",
                    occurred_at=action.created_at,
                    kind="APPROVAL_GATE_CREATED",
                    source=task_endpoint,
                    target=target,
                    transport="POLICY",
                    payload_kind="GOVERNED_ACTION",
                    status=action.approval_status.value,
                    summary=safe,
                )
            )
            if action.decided_at:
                events.append(
                    InteractionEvent(
                        id=f"approval:{action.id}:decided",
                        occurred_at=action.decided_at,
                        kind="APPROVAL_GATE_DECIDED",
                        source=target,
                        target=task_endpoint,
                        transport="POLICY",
                        payload_kind="GOVERNED_ACTION",
                        status=action.approval_status.value,
                        summary=safe,
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
