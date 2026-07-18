from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any
from uuid import UUID

from agentmesh.application.budget_services import BudgetController
from agentmesh.application.coordination_services import CoordinatedScheduler
from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.application.services import TaskApplicationService
from agentmesh.domain.budgets import TaskBudget
from agentmesh.domain.coordination import SubtaskStatus
from agentmesh.domain.errors import (
    IdempotencyConflict,
    InvalidTaskInput,
    InvalidTaskTransition,
    TaskNotFound,
)
from agentmesh.domain.messaging import IdempotencyRecord, MessageEnvelope
from agentmesh.domain.resolutions import TaskResolution, TaskResolutionAction
from agentmesh.domain.tasks import (
    ReviewDecision,
    RunRole,
    RunStatus,
    Task,
    TaskAggregate,
    TaskExecutionMode,
    TaskRun,
    utc_now,
)
from agentmesh.features import Feature, FeatureGateSet


@dataclass(frozen=True)
class TaskResolutionResult:
    resolution: TaskResolution
    aggregate: TaskAggregate


class TaskResolutionService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        tenant_id: str,
        executor_agent_id: str,
        reviewer_agent_id: str,
        supervisor_agent_id: str,
        feature_gates: FeatureGateSet,
    ) -> None:
        self._uow_factory = uow_factory
        self._tenant_id = tenant_id
        self._executor_agent_id = executor_agent_id
        self._reviewer_agent_id = reviewer_agent_id
        self._scheduler = CoordinatedScheduler(supervisor_agent_id=supervisor_agent_id)
        self._feature_gates = feature_gates

    def accept_candidate(
        self,
        task_id: UUID,
        *,
        actor: str,
        reason: str,
        idempotency_key: str | None = None,
    ) -> TaskResolutionResult:
        return self._resolve_simple(
            task_id,
            action=TaskResolutionAction.ACCEPT_CANDIDATE,
            actor=actor,
            reason=reason,
            idempotency_key=idempotency_key,
        )

    def reject_task(
        self,
        task_id: UUID,
        *,
        actor: str,
        reason: str,
        idempotency_key: str | None = None,
    ) -> TaskResolutionResult:
        return self._resolve_simple(
            task_id,
            action=TaskResolutionAction.REJECT_TASK,
            actor=actor,
            reason=reason,
            idempotency_key=idempotency_key,
        )

    def increase_budget_and_resume(
        self,
        task_id: UUID,
        *,
        replacement: TaskBudget,
        actor: str,
        reason: str,
        idempotency_key: str | None = None,
    ) -> TaskResolutionResult:
        self._feature_gates.require(Feature.HUMAN_RESOLUTION)
        self._feature_gates.require(Feature.BUDGET_ADMISSION)
        request = {
            "task_id": str(task_id),
            "action": TaskResolutionAction.INCREASE_BUDGET_AND_RESUME.value,
            "actor": actor,
            "reason": reason,
            "replacement": replacement.to_dict(),
        }
        scope, key, request_hash = self._command_identity(task_id, request, idempotency_key)
        with self._uow_factory() as uow:
            replay = self._idempotent_replay(uow, scope, key, request_hash)
            if replay is not None:
                return self._replay_result(uow, task_id, replay)
            task = self._task_or_raise(uow, task_id, for_update=True)
            previous_status = task.status
            previous_error = task.error
            previous_budget = task.budget.to_dict() if task.budget is not None else None
            previous_revision = task.budget_revision
            task.increase_budget(replacement)
            resumed_run = self._resume_after_budget(uow, task)
            resolution = TaskResolution.create(
                task_id=task.id,
                action=TaskResolutionAction.INCREASE_BUDGET_AND_RESUME,
                actor=actor,
                reason=reason,
                previous_status=previous_status,
                resulting_status=task.status,
                previous_error=previous_error,
                details={
                    "previous_budget": previous_budget,
                    "replacement_budget": replacement.to_dict(),
                    "previous_budget_revision": previous_revision,
                    "budget_revision": task.budget_revision,
                    "resumed_run_id": str(resumed_run.id) if resumed_run else None,
                },
            )
            self._persist_resolution(uow, task, resolution, scope, key, request_hash)
            uow.commit()
            return TaskResolutionResult(resolution, self._aggregate(uow, task))

    def list_resolutions(self, task_id: UUID) -> list[TaskResolution]:
        self._feature_gates.require(Feature.HUMAN_RESOLUTION)
        with self._uow_factory() as uow:
            self._task_or_raise(uow, task_id)
            return uow.task_resolutions.list_for_task(task_id)

    def _resolve_simple(
        self,
        task_id: UUID,
        *,
        action: TaskResolutionAction,
        actor: str,
        reason: str,
        idempotency_key: str | None,
    ) -> TaskResolutionResult:
        self._feature_gates.require(Feature.HUMAN_RESOLUTION)
        request = {
            "task_id": str(task_id),
            "action": action.value,
            "actor": actor,
            "reason": reason,
        }
        scope, key, request_hash = self._command_identity(task_id, request, idempotency_key)
        with self._uow_factory() as uow:
            replay = self._idempotent_replay(uow, scope, key, request_hash)
            if replay is not None:
                return self._replay_result(uow, task_id, replay)
            task = self._task_or_raise(uow, task_id, for_update=True)
            previous_status = task.status
            previous_error = task.error
            if action == TaskResolutionAction.ACCEPT_CANDIDATE:
                task.accept_waiting_candidate()
            else:
                task.reject_waiting()
            resolution = TaskResolution.create(
                task_id=task.id,
                action=action,
                actor=actor,
                reason=reason,
                previous_status=previous_status,
                resulting_status=task.status,
                previous_error=previous_error,
            )
            self._persist_resolution(uow, task, resolution, scope, key, request_hash)
            uow.commit()
            return TaskResolutionResult(resolution, self._aggregate(uow, task))

    def _resume_after_budget(self, uow: Any, task: Task) -> TaskRun | None:
        runs = uow.runs.list_for_task(task.id)
        latest = runs[-1] if runs else None
        if task.execution_mode == TaskExecutionMode.DIRECT:
            if task.candidate_output is not None and latest is not None:
                task.accept_waiting_candidate()
                return None
            return self._queue_replacement(uow, task, latest, RunRole.EXECUTOR)
        if task.execution_mode == TaskExecutionMode.REVIEWED:
            return self._resume_reviewed(uow, task, latest)
        if (
            task.candidate_output is not None
            and latest is not None
            and latest.role == RunRole.SUPERVISOR
        ):
            task.accept_waiting_candidate()
            return None
        self._require_future_admission(uow, task)
        for subtask in uow.subtasks.list_for_task(task.id, for_update=True):
            if subtask.status == SubtaskStatus.CANCELED:
                subtask.reopen_after_budget()
                uow.subtasks.save(subtask)
        task.resume_waiting_coordination()
        created = self._scheduler.schedule(uow, task)
        return created[0] if created else None

    def _resume_reviewed(
        self,
        uow: Any,
        task: Task,
        latest: TaskRun | None,
    ) -> TaskRun | None:
        if latest is None or latest.status == RunStatus.CANCELED:
            role = latest.role if latest is not None else RunRole.EXECUTOR
            return self._queue_replacement(uow, task, latest, role)
        if latest.role == RunRole.EXECUTOR and latest.status == RunStatus.SUCCEEDED:
            return self._queue_replacement(uow, task, None, RunRole.REVIEWER)
        if latest.role != RunRole.REVIEWER or latest.output is None:
            raise InvalidTaskTransition("Reviewed Task has no deterministic resume point")
        decision = ReviewDecision.from_output(latest.output, task.acceptance_criteria)
        if decision.accepted:
            task.latest_review = decision.to_dict()
            task.accept_waiting_candidate()
            return None
        within_deadline = task.review_deadline is None or utc_now() < task.review_deadline
        if task.revision_count >= task.max_revisions or not within_deadline:
            error = (
                "review_revision_limit_reached"
                if task.revision_count >= task.max_revisions
                else "review_deadline_exceeded"
            )
            task.remain_waiting_after_review(decision, error)
            return None
        self._require_future_admission(uow, task)
        executor_name, executor_version = TaskApplicationService._resolve_agent_by_name(
            uow, task.tenant_id, self._executor_agent_id
        )
        run = TaskRun.request(
            task.id,
            executor_name,
            agent_version_id=executor_version.id,
            agent_version_digest=executor_version.content_digest,
            role=RunRole.EXECUTOR,
            revision_number=task.revision_count + 1,
        )
        task.resume_waiting_revision(run.id, decision)
        self._persist_run(uow, task, run)
        return run

    def _queue_replacement(
        self,
        uow: Any,
        task: Task,
        previous: TaskRun | None,
        role: RunRole,
    ) -> TaskRun:
        self._require_future_admission(uow, task)
        if previous is not None:
            run = TaskRun.request(
                task.id,
                previous.agent_id,
                agent_version_id=previous.agent_version_id,
                agent_version_digest=previous.agent_version_digest,
                role=role,
                revision_number=previous.revision_number,
            )
        else:
            configured = (
                self._reviewer_agent_id if role == RunRole.REVIEWER else self._executor_agent_id
            )
            agent_name, agent_version = TaskApplicationService._resolve_agent_by_name(
                uow, task.tenant_id, configured
            )
            run = TaskRun.request(
                task.id,
                agent_name,
                agent_version_id=agent_version.id,
                agent_version_digest=agent_version.content_digest,
                role=role,
                revision_number=task.revision_count,
            )
        task.resume_waiting_with_run(run.id, reviewing=role == RunRole.REVIEWER)
        self._persist_run(uow, task, run)
        return run

    @staticmethod
    def _require_future_admission(uow: Any, task: Task) -> None:
        rejection = BudgetController.run_rejection(uow, task)
        if rejection is None:
            rejection = BudgetController.attempt_rejection(uow, task)
        if rejection is not None:
            raise InvalidTaskInput(f"Replacement budget still blocks resume: {rejection}")

    @staticmethod
    def _persist_run(uow: Any, task: Task, run: TaskRun) -> None:
        uow.runs.add(run)
        uow.outbox.add(
            MessageEnvelope.run_requested(
                tenant_id=task.tenant_id,
                task_id=task.id,
                run_id=run.id,
            )
        )

    def _persist_resolution(
        self,
        uow: Any,
        task: Task,
        resolution: TaskResolution,
        scope: str,
        key: str,
        request_hash: str,
    ) -> None:
        uow.tasks.save(task)
        uow.task_resolutions.add(resolution)
        uow.outbox.add(
            MessageEnvelope.domain_event(
                schema_name="agentmesh.task.resolved",
                tenant_id=task.tenant_id,
                aggregate_id=task.id,
                causation_id=resolution.id,
                payload={
                    "task_id": str(task.id),
                    "resolution_id": str(resolution.id),
                    "action": resolution.action.value,
                    "actor": resolution.actor,
                    "resulting_status": resolution.resulting_status.value,
                },
            )
        )
        if key:
            uow.idempotency.add(
                IdempotencyRecord.create(
                    scope=scope,
                    key=key,
                    request_hash=request_hash,
                    result={"resolution_id": str(resolution.id)},
                )
            )

    def _replay_result(
        self,
        uow: Any,
        task_id: UUID,
        replay: dict[str, Any],
    ) -> TaskResolutionResult:
        task = self._task_or_raise(uow, task_id)
        resolution = uow.task_resolutions.get(UUID(str(replay["resolution_id"])))
        if resolution is None:
            raise InvalidTaskTransition("Resolution idempotency record lost its result")
        return TaskResolutionResult(resolution, self._aggregate(uow, task))

    def _task_or_raise(self, uow: Any, task_id: UUID, *, for_update: bool = False) -> Task:
        task = uow.tasks.get(task_id, for_update=for_update)
        if task is None or task.tenant_id != self._tenant_id:
            raise TaskNotFound(task_id)
        return task

    @staticmethod
    def _aggregate(uow: Any, task: Task) -> TaskAggregate:
        return TaskAggregate(
            task=task,
            runs=uow.runs.list_for_task(task.id),
            attempts=uow.attempts.list_for_task(task.id),
            subtasks=uow.subtasks.list_for_task(task.id),
            dependencies=uow.subtask_dependencies.list_for_task(task.id),
            handoffs=uow.handoffs.list_for_task(task.id),
        )

    @staticmethod
    def _command_identity(
        task_id: UUID,
        request: dict[str, Any],
        idempotency_key: str | None,
    ) -> tuple[str, str, str]:
        key = (idempotency_key or "").strip()
        if idempotency_key is not None and not key:
            raise InvalidTaskInput("Idempotency-Key must not be blank")
        canonical = json.dumps(request, sort_keys=True, separators=(",", ":"))
        action = str(request["action"]).lower().replace("_", "-")
        return (
            f"task-resolution:{action}:{task_id}",
            key,
            sha256(canonical.encode()).hexdigest(),
        )

    @staticmethod
    def _idempotent_replay(
        uow: Any,
        scope: str,
        key: str,
        request_hash: str,
    ) -> dict[str, Any] | None:
        if not key:
            return None
        uow.idempotency.lock(scope, key)
        existing = uow.idempotency.get(scope, key)
        if existing is None:
            return None
        if existing.request_hash != request_hash:
            raise IdempotencyConflict(
                f"Idempotency key '{key}' was already used with a different request"
            )
        return dict(existing.result)
