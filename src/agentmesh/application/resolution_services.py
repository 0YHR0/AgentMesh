from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any
from uuid import UUID

from agentmesh.application.authority_cohorts import AuthorityCohortResolver, ContinuationKind
from agentmesh.application.budget_services import BudgetController
from agentmesh.application.coordinated_runtime import (
    CoordinatedRuntimeAggregate,
    CoordinatedRuntimeAggregateLocker,
)
from agentmesh.application.coordination_services import CoordinatedScheduler
from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.application.services import TaskApplicationService
from agentmesh.domain.budgets import TaskBudget
from agentmesh.domain.coordination import (
    TERMINAL_SUBTASK_STATUSES,
    CoordinationRuntimeBoundary,
    CoordinationRuntimeDrainStatus,
    CoordinationRuntimeDrainTarget,
    SubtaskCancellationSource,
    SubtaskStatus,
)
from agentmesh.domain.errors import (
    IdempotencyConflict,
    InvalidTaskInput,
    InvalidTaskTransition,
    TaskNotFound,
)
from agentmesh.domain.messaging import (
    RUN_REQUESTED_SCHEMA,
    RUN_REQUESTED_VERSION,
    IdempotencyRecord,
    MessageEnvelope,
)
from agentmesh.domain.resolutions import TaskResolution, TaskResolutionAction
from agentmesh.domain.runtime_execution import (
    RuntimeExecutionPhase,
    RuntimeLifecycleStatus,
)
from agentmesh.domain.tasks import (
    AttemptStatus,
    ReviewDecision,
    RunRole,
    RunStatus,
    Task,
    TaskAggregate,
    TaskExecutionMode,
    TaskRun,
    TaskStatus,
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
        authority_cohort_resolver: AuthorityCohortResolver | None = None,
        coordinated_aggregate_locker: CoordinatedRuntimeAggregateLocker | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._tenant_id = tenant_id
        self._executor_agent_id = executor_agent_id
        self._reviewer_agent_id = reviewer_agent_id
        self._feature_gates = feature_gates
        self._coordinated_aggregate_locker = (
            coordinated_aggregate_locker or CoordinatedRuntimeAggregateLocker()
        )
        self._authority_cohort_resolver = authority_cohort_resolver or AuthorityCohortResolver(
            feature_gates=feature_gates,
        )
        self._scheduler = CoordinatedScheduler(
            supervisor_agent_id=supervisor_agent_id,
            authority_cohort_resolver=self._authority_cohort_resolver,
        )

    def accept_candidate(
        self,
        task_id: UUID,
        *,
        actor: str,
        reason: str,
        idempotency_key: str | None = None,
    ) -> TaskResolutionResult:
        self._feature_gates.require(Feature.HUMAN_RESOLUTION)
        request = {
            "task_id": str(task_id),
            "action": TaskResolutionAction.ACCEPT_CANDIDATE.value,
            "actor": actor,
            "reason": reason,
        }
        scope, key, request_hash = self._command_identity(task_id, request, idempotency_key)
        with self._uow_factory() as uow:
            task = self._task_or_raise(uow, task_id, for_update=True)
            if task.execution_mode is TaskExecutionMode.COORDINATED:
                aggregate = self._coordinated_aggregate_locker.lock_after_task(
                    uow,
                    task,
                    tenant_id=self._tenant_id,
                    task_id=task_id,
                )
                replay = self._idempotent_replay(uow, scope, key, request_hash)
                if replay is not None:
                    return self._replay_coordinated_candidate(
                        uow,
                        aggregate=aggregate,
                        replay=replay,
                        actor=actor,
                        reason=reason,
                    )
                return self._apply_coordinated_candidate(
                    uow,
                    aggregate=aggregate,
                    actor=actor,
                    reason=reason,
                    scope=scope,
                    key=key,
                    request_hash=request_hash,
                )
            replay = self._idempotent_replay(uow, scope, key, request_hash)
            if replay is not None:
                return self._replay_result(uow, task_id, replay)
            previous_status = task.status
            previous_error = task.error
            task.accept_waiting_candidate()
            resolution = TaskResolution.create(
                task_id=task.id,
                action=TaskResolutionAction.ACCEPT_CANDIDATE,
                actor=actor,
                reason=reason,
                previous_status=previous_status,
                resulting_status=task.status,
                previous_error=previous_error,
            )
            self._persist_resolution(uow, task, resolution, scope, key, request_hash)
            uow.commit()
            return TaskResolutionResult(resolution, self._aggregate(uow, task))

    def _apply_coordinated_candidate(
        self,
        uow: Any,
        *,
        aggregate: CoordinatedRuntimeAggregate,
        actor: str,
        reason: str,
        scope: str,
        key: str,
        request_hash: str,
    ) -> TaskResolutionResult:
        task, drain, supervisor = self._validate_coordinated_candidate(
            aggregate,
            completed=False,
        )
        previous_status = task.status
        previous_error = task.error
        candidate_digest = self._candidate_digest(task.candidate_output)
        resolution_at = max(utc_now(), task.updated_at, drain.updated_at)
        resolution = TaskResolution.create(
            task_id=task.id,
            action=TaskResolutionAction.ACCEPT_CANDIDATE,
            actor=actor,
            reason=reason,
            previous_status=previous_status,
            resulting_status=TaskStatus.COMPLETED,
            previous_error=previous_error,
            details={
                "coordination_runtime_drain_id": str(drain.id),
                "supervisor_run_id": str(supervisor.id),
                "candidate_digest": candidate_digest,
            },
            at=resolution_at,
        )
        completed_drain = drain.complete(at=resolution.created_at)
        if completed_drain is drain:
            raise InvalidTaskTransition("Candidate approval drain is already complete")
        task.accept_waiting_candidate(at=resolution.created_at)
        event = self._resolution_event(task, resolution)
        uow.coordination_runtime_drains.save(
            completed_drain,
            tenant_id=task.tenant_id,
        )
        uow.tasks.save(task)
        uow.task_resolutions.add(resolution)
        uow.outbox.add(event)
        if key:
            uow.idempotency.add(
                IdempotencyRecord.create(
                    scope=scope,
                    key=key,
                    request_hash=request_hash,
                    result={
                        "resolution_id": str(resolution.id),
                        "drain_id": str(completed_drain.id),
                        "supervisor_run_id": str(supervisor.id),
                        "candidate_digest": candidate_digest,
                        "outbox_event_id": str(event.message_id),
                    },
                )
            )
        uow.commit()
        return TaskResolutionResult(resolution, self._aggregate(uow, task))

    def _replay_coordinated_candidate(
        self,
        uow: Any,
        *,
        aggregate: CoordinatedRuntimeAggregate,
        replay: dict[str, Any],
        actor: str,
        reason: str,
    ) -> TaskResolutionResult:
        expected_keys = {
            "resolution_id",
            "drain_id",
            "supervisor_run_id",
            "candidate_digest",
            "outbox_event_id",
        }
        if type(replay) is not dict or set(replay) != expected_keys:
            raise InvalidTaskTransition("Resolution idempotency projection is invalid")
        try:
            resolution_id = UUID(replay["resolution_id"])
            drain_id = UUID(replay["drain_id"])
            supervisor_id = UUID(replay["supervisor_run_id"])
            event_id = UUID(replay["outbox_event_id"])
        except (TypeError, ValueError) as exc:
            raise InvalidTaskTransition(
                "Resolution idempotency projection is invalid"
            ) from exc
        task, _active_drain, supervisor = self._validate_coordinated_candidate(
            aggregate,
            completed=True,
        )
        drain = uow.coordination_runtime_drains.get(
            drain_id,
            tenant_id=task.tenant_id,
            for_update=True,
        )
        candidate_digest = self._candidate_digest(task.candidate_output)
        if (
            drain is None
            or drain.task_id != task.id
            or drain.status is not CoordinationRuntimeDrainStatus.COMPLETE
            or drain.target is not CoordinationRuntimeDrainTarget.WAITING_APPROVAL
            or supervisor.id != supervisor_id
            or replay["candidate_digest"] != candidate_digest
        ):
            raise InvalidTaskTransition("Resolution replay projection is inconsistent")
        resolution = uow.task_resolutions.get(resolution_id)
        expected_details = {
            "coordination_runtime_drain_id": str(drain.id),
            "supervisor_run_id": str(supervisor.id),
            "candidate_digest": candidate_digest,
        }
        if (
            resolution is None
            or resolution.task_id != task.id
            or resolution.action is not TaskResolutionAction.ACCEPT_CANDIDATE
            or resolution.actor != actor.strip()
            or resolution.reason != reason.strip()
            or resolution.previous_status is not TaskStatus.WAITING_APPROVAL
            or resolution.resulting_status is not TaskStatus.COMPLETED
            or resolution.previous_error != drain.reason
            or resolution.details != expected_details
        ):
            raise InvalidTaskTransition("Resolution replay audit is inconsistent")
        event = uow.outbox.get(event_id, tenant_id=task.tenant_id)
        if event is None or event.to_dict() != self._resolution_event(
            task,
            resolution,
            message_id=event_id,
        ).to_dict():
            raise InvalidTaskTransition("Resolution replay Outbox is inconsistent")
        return TaskResolutionResult(resolution, self._aggregate(uow, task))

    @staticmethod
    def _validate_coordinated_candidate(
        aggregate: CoordinatedRuntimeAggregate,
        *,
        completed: bool,
    ) -> tuple[Task, Any, TaskRun]:
        task = aggregate.task
        if aggregate.cohort.runtime_authority != "managed":
            raise InvalidTaskTransition("Candidate approval requires managed Runtime authority")
        expected_status = TaskStatus.COMPLETED if completed else TaskStatus.WAITING_APPROVAL
        if (
            task.status is not expected_status
            or task.current_run_id is not None
            or task.candidate_output is None
            or type(task.candidate_output) is not dict
        ):
            raise InvalidTaskTransition("Coordinated candidate Task projection is invalid")
        if completed:
            if (
                task.output != task.candidate_output
                or task.error is not None
                or task.budget_exhausted_reason is not None
                or aggregate.active_drain is not None
            ):
                raise InvalidTaskTransition("Completed candidate Task projection is invalid")
            drain = None
        else:
            drain = aggregate.active_drain
            if (
                task.output is not None
                or type(task.error) is not str
                or not task.error
                or task.budget_exhausted_reason != task.error
                or drain is None
                or drain.status is not CoordinationRuntimeDrainStatus.DRAINING
                or drain.target is not CoordinationRuntimeDrainTarget.WAITING_APPROVAL
                or drain.reason != task.error
            ):
                raise InvalidTaskTransition("Waiting candidate drain projection is invalid")
        terminal_runs = {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELED}
        terminal_attempts = {
            AttemptStatus.SUCCEEDED,
            AttemptStatus.FAILED,
            AttemptStatus.CANCELED,
        }
        terminal_runtime = {
            RuntimeExecutionPhase.SUCCEEDED,
            RuntimeExecutionPhase.FAILED,
            RuntimeExecutionPhase.CANCELED,
            RuntimeExecutionPhase.TIMED_OUT,
        }
        run_ids = {value.id for value in aggregate.runs}
        if (
            set(aggregate.latest_attempts) != run_ids
            or set(aggregate.boundary_classifications) != run_ids
            or any(
                value.status not in TERMINAL_SUBTASK_STATUSES
                for value in aggregate.subtasks
            )
            or any(value.status not in terminal_runs for value in aggregate.runs)
            or any(
                value is not None and value.status not in terminal_attempts
                for value in aggregate.latest_attempts.values()
            )
            or any(value.phase not in terminal_runtime for value in aggregate.executions)
            or any(
                value.status
                in {RuntimeLifecycleStatus.REQUESTED, RuntimeLifecycleStatus.ACCEPTED}
                for value in aggregate.lifecycle_operations
            )
            or any(
                value is not CoordinationRuntimeBoundary.KNOWN_TERMINAL
                for value in aggregate.boundary_classifications.values()
            )
        ):
            raise InvalidTaskTransition("Coordinated candidate has unfinished siblings")
        supervisors = tuple(
            value
            for value in aggregate.runs
            if value.role is RunRole.SUPERVISOR
            and value.status is RunStatus.SUCCEEDED
            and value.output == task.candidate_output
        )
        if len(supervisors) != 1:
            raise InvalidTaskTransition("Coordinated candidate Supervisor is ambiguous")
        supervisor = supervisors[0]
        supervisor_attempt = aggregate.latest_attempts.get(supervisor.id)
        supervisor_executions = aggregate.executions_by_run.get(supervisor.id, ())
        if (
            supervisor_attempt is None
            or supervisor_attempt.status is not AttemptStatus.SUCCEEDED
            or len(supervisor_executions) != 1
            or supervisor_executions[0].phase is not RuntimeExecutionPhase.SUCCEEDED
        ):
            raise InvalidTaskTransition("Coordinated candidate Supervisor evidence is invalid")
        return task, drain, supervisor

    @staticmethod
    def _candidate_digest(candidate: dict[str, Any] | None) -> str:
        if type(candidate) is not dict:
            raise InvalidTaskTransition("Coordinated candidate output is invalid")
        canonical = json.dumps(candidate, sort_keys=True, separators=(",", ":"))
        return sha256(canonical.encode()).hexdigest()

    @staticmethod
    def _resolution_event(
        task: Task,
        resolution: TaskResolution,
        *,
        message_id: UUID | None = None,
    ) -> MessageEnvelope:
        event = MessageEnvelope.domain_event(
            schema_name="agentmesh.task.resolved",
            tenant_id=task.tenant_id,
            aggregate_id=task.id,
            causation_id=resolution.id,
            at=resolution.created_at,
            payload={
                "task_id": str(task.id),
                "resolution_id": str(resolution.id),
                "action": resolution.action.value,
                "actor": resolution.actor,
                "resulting_status": resolution.resulting_status.value,
            },
        )
        if message_id is None:
            return event
        return MessageEnvelope(
            schema_name=event.schema_name,
            schema_version=event.schema_version,
            message_id=message_id,
            tenant_id=event.tenant_id,
            occurred_at=event.occurred_at,
            producer=event.producer,
            correlation_id=event.correlation_id,
            causation_id=event.causation_id,
            idempotency_key=f"event:{message_id}",
            payload=dict(event.payload),
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
            task = self._task_or_raise(uow, task_id, for_update=True)
            aggregate = None
            if task.execution_mode is TaskExecutionMode.COORDINATED and hasattr(
                uow, "coordination_runtime_drains"
            ):
                aggregate = self._coordinated_aggregate_locker.lock_after_task(
                    uow,
                    task,
                    tenant_id=self._tenant_id,
                    task_id=task_id,
                )
            replay = self._idempotent_replay(uow, scope, key, request_hash)
            if replay is not None:
                if aggregate is not None and aggregate.cohort.runtime_authority == "managed":
                    if "supervisor_run_id" in replay:
                        return self._replay_coordinated_candidate_budget_resume(
                            uow,
                            aggregate=aggregate,
                            replay=replay,
                            actor=actor,
                            reason=reason,
                        )
                    return self._replay_coordinated_budget_resume(
                        uow,
                        aggregate=aggregate,
                        replay=replay,
                        actor=actor,
                        reason=reason,
                    )
                return self._replay_result(uow, task_id, replay)
            if aggregate is not None and aggregate.cohort.runtime_authority == "managed":
                if aggregate.task.candidate_output is not None:
                    return self._apply_coordinated_candidate_budget_resume(
                        uow,
                        aggregate=aggregate,
                        replacement=replacement,
                        actor=actor,
                        reason=reason,
                        scope=scope,
                        key=key,
                        request_hash=request_hash,
                    )
                return self._apply_coordinated_budget_resume(
                    uow,
                    aggregate=aggregate,
                    replacement=replacement,
                    actor=actor,
                    reason=reason,
                    scope=scope,
                    key=key,
                    request_hash=request_hash,
                )
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

    def _apply_coordinated_candidate_budget_resume(
        self,
        uow: Any,
        *,
        aggregate: CoordinatedRuntimeAggregate,
        replacement: TaskBudget,
        actor: str,
        reason: str,
        scope: str,
        key: str,
        request_hash: str,
    ) -> TaskResolutionResult:
        task, drain, supervisor = self._validate_coordinated_candidate(
            aggregate,
            completed=False,
        )
        if drain.triggering_run_id != supervisor.id:
            raise InvalidTaskTransition(
                "Coordinated candidate budget drain does not belong to Supervisor"
            )
        previous_status = task.status
        previous_error = task.error
        previous_budget = task.budget.to_dict() if task.budget is not None else None
        previous_revision = task.budget_revision
        candidate_digest = self._candidate_digest(task.candidate_output)
        operation_at = max(utc_now(), task.updated_at, drain.updated_at)
        task.increase_budget(replacement, at=operation_at)
        task.accept_waiting_candidate(at=operation_at)
        completed_drain = drain.complete(at=operation_at)
        if completed_drain is drain:
            raise InvalidTaskTransition("Budget candidate drain is already complete")
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
                "coordination_runtime_drain_id": str(drain.id),
                "drain_version_before": drain.version,
                "drain_version_after": completed_drain.version,
                "supervisor_run_id": str(supervisor.id),
                "candidate_digest": candidate_digest,
                "reopened_subtask_ids": [],
                "scheduled_run_ids": [],
                "run_requested_event_ids": [],
            },
            at=operation_at,
        )
        event = self._resolution_event(task, resolution)
        uow.coordination_runtime_drains.save(
            completed_drain,
            tenant_id=task.tenant_id,
        )
        uow.tasks.save(task)
        uow.task_resolutions.add(resolution)
        uow.outbox.add(event)
        if key:
            uow.idempotency.add(
                IdempotencyRecord.create(
                    scope=scope,
                    key=key,
                    request_hash=request_hash,
                    result={
                        "resolution_id": str(resolution.id),
                        "drain_id": str(completed_drain.id),
                        "drain_version_before": drain.version,
                        "drain_version_after": completed_drain.version,
                        "previous_budget": previous_budget,
                        "replacement_budget": replacement.to_dict(),
                        "previous_budget_revision": previous_revision,
                        "resulting_budget_revision": task.budget_revision,
                        "supervisor_run_id": str(supervisor.id),
                        "candidate_digest": candidate_digest,
                        "reopened_subtask_ids": [],
                        "scheduled_run_ids": [],
                        "run_requested_event_ids": [],
                        "outbox_event_id": str(event.message_id),
                    },
                )
            )
        uow.commit()
        return TaskResolutionResult(resolution, self._aggregate(uow, task))

    def _apply_coordinated_budget_resume(
        self,
        uow: Any,
        *,
        aggregate: CoordinatedRuntimeAggregate,
        replacement: TaskBudget,
        actor: str,
        reason: str,
        scope: str,
        key: str,
        request_hash: str,
    ) -> TaskResolutionResult:
        task = aggregate.task
        drain, reopen = self._validate_coordinated_budget_resume(aggregate)
        previous_status = task.status
        previous_error = task.error
        previous_budget = task.budget.to_dict() if task.budget is not None else None
        previous_revision = task.budget_revision
        operation_at = max(
            utc_now(),
            task.updated_at,
            drain.updated_at,
            *(value.updated_at for value in reopen),
        )
        task.increase_budget(replacement, at=operation_at)
        self._require_future_admission(uow, task)
        reopened_ids: list[UUID] = []
        for subtask in reopen:
            subtask.reopen_after_budget_drain(drain.id, at=operation_at)
            uow.subtasks.save(subtask)
            reopened_ids.append(subtask.id)
        task.resume_waiting_coordination(at=operation_at)
        uow.tasks.save(task)
        schedule_receipt = self._scheduler.schedule_with_receipt(
            uow,
            task,
            at=operation_at,
        )
        scheduled = schedule_receipt.runs
        scheduled_ids = tuple(sorted((value.id for value in scheduled), key=str))
        receipt_run_ids: list[UUID] = []
        for event in schedule_receipt.run_requested_events:
            try:
                receipt_run_ids.append(UUID(str(event.payload.get("run_id"))))
            except (TypeError, ValueError) as exc:
                raise InvalidTaskTransition(
                    "Coordinated scheduler receipt is invalid"
                ) from exc
            if (
                event.schema_name != RUN_REQUESTED_SCHEMA
                or event.schema_version != RUN_REQUESTED_VERSION
                or event.tenant_id != task.tenant_id
                or event.producer != "agentmesh-control-api"
                or event.correlation_id != task.id
                or event.causation_id is not None
                or event.occurred_at != operation_at
                or event.payload.get("task_id") != str(task.id)
                or event.idempotency_key != f"run:{event.payload.get('run_id')}"
            ):
                raise InvalidTaskTransition("Coordinated scheduler receipt is invalid")
        if sorted(receipt_run_ids, key=str) != list(scheduled_ids):
            raise InvalidTaskTransition("Coordinated scheduler receipt is invalid")
        completed_drain = drain.complete(at=operation_at)
        if completed_drain is drain:
            raise InvalidTaskTransition("Budget resume drain is already complete")
        uow.coordination_runtime_drains.save(
            completed_drain,
            tenant_id=task.tenant_id,
        )
        run_requested_event_ids = tuple(
            sorted(
                (value.message_id for value in schedule_receipt.run_requested_events),
                key=str,
            )
        )
        reopened = tuple(sorted(reopened_ids, key=str))
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
                "coordination_runtime_drain_id": str(drain.id),
                "drain_version_before": drain.version,
                "drain_version_after": completed_drain.version,
                "reopened_subtask_ids": [str(value) for value in reopened],
                "scheduled_run_ids": [str(value) for value in scheduled_ids],
            },
            at=operation_at,
        )
        event = self._resolution_event(task, resolution)
        uow.tasks.save(task)
        uow.task_resolutions.add(resolution)
        uow.outbox.add(event)
        if key:
            uow.idempotency.add(
                IdempotencyRecord.create(
                    scope=scope,
                    key=key,
                    request_hash=request_hash,
                    result={
                        "resolution_id": str(resolution.id),
                        "drain_id": str(completed_drain.id),
                        "drain_version_before": drain.version,
                        "drain_version_after": completed_drain.version,
                        "previous_budget": previous_budget,
                        "replacement_budget": replacement.to_dict(),
                        "previous_budget_revision": previous_revision,
                        "resulting_budget_revision": task.budget_revision,
                        "reopened_subtask_ids": [str(value) for value in reopened],
                        "scheduled_run_ids": [str(value) for value in scheduled_ids],
                        "run_requested_event_ids": [
                            str(value) for value in run_requested_event_ids
                        ],
                        "outbox_event_id": str(event.message_id),
                    },
                )
            )
        uow.commit()
        return TaskResolutionResult(resolution, self._aggregate(uow, task))

    def _replay_coordinated_candidate_budget_resume(
        self,
        uow: Any,
        *,
        aggregate: CoordinatedRuntimeAggregate,
        replay: dict[str, Any],
        actor: str,
        reason: str,
    ) -> TaskResolutionResult:
        expected_keys = {
            "resolution_id",
            "drain_id",
            "drain_version_before",
            "drain_version_after",
            "previous_budget",
            "replacement_budget",
            "previous_budget_revision",
            "resulting_budget_revision",
            "supervisor_run_id",
            "candidate_digest",
            "reopened_subtask_ids",
            "scheduled_run_ids",
            "run_requested_event_ids",
            "outbox_event_id",
        }
        if type(replay) is not dict or set(replay) != expected_keys:
            raise InvalidTaskTransition("Resolution idempotency projection is invalid")
        try:
            resolution_id = UUID(replay["resolution_id"])
            drain_id = UUID(replay["drain_id"])
            supervisor_id = UUID(replay["supervisor_run_id"])
            event_id = UUID(replay["outbox_event_id"])
        except (TypeError, ValueError) as exc:
            raise InvalidTaskTransition("Resolution idempotency projection is invalid") from exc
        revisions = (
            replay["drain_version_before"],
            replay["drain_version_after"],
            replay["previous_budget_revision"],
            replay["resulting_budget_revision"],
        )
        if (
            any(type(value) is not int or value < 0 for value in revisions)
            or replay["drain_version_after"] != replay["drain_version_before"] + 1
            or replay["resulting_budget_revision"] != replay["previous_budget_revision"] + 1
            or replay["reopened_subtask_ids"] != []
            or replay["scheduled_run_ids"] != []
            or replay["run_requested_event_ids"] != []
        ):
            raise InvalidTaskTransition("Resolution idempotency projection is invalid")
        task, _active_drain, supervisor = self._validate_coordinated_candidate(
            aggregate,
            completed=True,
        )
        drain = uow.coordination_runtime_drains.get(
            drain_id,
            tenant_id=task.tenant_id,
            for_update=True,
        )
        candidate_digest = self._candidate_digest(task.candidate_output)
        if (
            drain is None
            or drain.task_id != task.id
            or drain.status is not CoordinationRuntimeDrainStatus.COMPLETE
            or drain.target is not CoordinationRuntimeDrainTarget.WAITING_APPROVAL
            or drain.version != replay["drain_version_after"]
            or drain.triggering_run_id != supervisor.id
            or supervisor.id != supervisor_id
            or replay["candidate_digest"] != candidate_digest
            or task.budget is None
            or task.budget.to_dict() != replay["replacement_budget"]
            or task.budget_revision != replay["resulting_budget_revision"]
        ):
            raise InvalidTaskTransition("Resolution replay projection is inconsistent")
        resolution = uow.task_resolutions.get(resolution_id)
        expected_details = {
            "previous_budget": replay["previous_budget"],
            "replacement_budget": replay["replacement_budget"],
            "previous_budget_revision": replay["previous_budget_revision"],
            "budget_revision": replay["resulting_budget_revision"],
            "coordination_runtime_drain_id": str(drain.id),
            "drain_version_before": replay["drain_version_before"],
            "drain_version_after": replay["drain_version_after"],
            "supervisor_run_id": str(supervisor.id),
            "candidate_digest": candidate_digest,
            "reopened_subtask_ids": [],
            "scheduled_run_ids": [],
            "run_requested_event_ids": [],
        }
        if (
            resolution is None
            or resolution.task_id != task.id
            or resolution.action is not TaskResolutionAction.INCREASE_BUDGET_AND_RESUME
            or resolution.actor != actor.strip()
            or resolution.reason != reason.strip()
            or resolution.previous_status is not TaskStatus.WAITING_APPROVAL
            or resolution.resulting_status is not TaskStatus.COMPLETED
            or resolution.previous_error != drain.reason
            or resolution.details != expected_details
        ):
            raise InvalidTaskTransition("Resolution replay audit is inconsistent")
        event = uow.outbox.get(event_id, tenant_id=task.tenant_id)
        if (
            event is None
            or event.to_dict()
            != self._resolution_event(
                task,
                resolution,
                message_id=event_id,
            ).to_dict()
        ):
            raise InvalidTaskTransition("Resolution replay Outbox is inconsistent")
        return TaskResolutionResult(resolution, self._aggregate(uow, task))

    def _replay_coordinated_budget_resume(
        self,
        uow: Any,
        *,
        aggregate: CoordinatedRuntimeAggregate,
        replay: dict[str, Any],
        actor: str,
        reason: str,
    ) -> TaskResolutionResult:
        expected_keys = {
            "resolution_id",
            "drain_id",
            "drain_version_before",
            "drain_version_after",
            "previous_budget",
            "replacement_budget",
            "previous_budget_revision",
            "resulting_budget_revision",
            "reopened_subtask_ids",
            "scheduled_run_ids",
            "run_requested_event_ids",
            "outbox_event_id",
        }
        if type(replay) is not dict or set(replay) != expected_keys:
            raise InvalidTaskTransition("Resolution idempotency projection is invalid")
        try:
            resolution_id = UUID(replay["resolution_id"])
            drain_id = UUID(replay["drain_id"])
            event_id = UUID(replay["outbox_event_id"])
            reopened_ids = tuple(UUID(value) for value in replay["reopened_subtask_ids"])
            scheduled_ids = tuple(UUID(value) for value in replay["scheduled_run_ids"])
            run_requested_event_ids = tuple(
                UUID(value) for value in replay["run_requested_event_ids"]
            )
        except (TypeError, ValueError) as exc:
            raise InvalidTaskTransition("Resolution idempotency projection is invalid") from exc
        revisions = (
            replay["drain_version_before"],
            replay["drain_version_after"],
            replay["previous_budget_revision"],
            replay["resulting_budget_revision"],
        )
        if (
            any(type(value) is not int or value < 0 for value in revisions)
            or replay["drain_version_after"] != replay["drain_version_before"] + 1
            or replay["resulting_budget_revision"] != replay["previous_budget_revision"] + 1
            or list(reopened_ids) != sorted(set(reopened_ids), key=str)
            or list(scheduled_ids) != sorted(set(scheduled_ids), key=str)
            or list(run_requested_event_ids) != sorted(set(run_requested_event_ids), key=str)
            or len(scheduled_ids) != len(run_requested_event_ids)
        ):
            raise InvalidTaskTransition("Resolution idempotency projection is invalid")
        task = aggregate.task
        drain = uow.coordination_runtime_drains.get(
            drain_id,
            tenant_id=task.tenant_id,
            for_update=True,
        )
        runs = {value.id: value for value in aggregate.runs}
        subtasks = {value.id: value for value in aggregate.subtasks}
        scheduled = tuple(runs.get(value) for value in scheduled_ids)
        reopened = tuple(subtasks.get(value) for value in reopened_ids)
        if (
            task.status is not TaskStatus.RUNNING
            or task.current_run_id is not None
            or task.output is not None
            or task.candidate_output is not None
            or task.error is not None
            or task.budget_exhausted_reason is not None
            or task.budget is None
            or task.budget.to_dict() != replay["replacement_budget"]
            or task.budget_revision != replay["resulting_budget_revision"]
            or aggregate.active_drain is not None
            or drain is None
            or drain.task_id != task.id
            or drain.status is not CoordinationRuntimeDrainStatus.COMPLETE
            or drain.target is not CoordinationRuntimeDrainTarget.WAITING_APPROVAL
            or drain.version != replay["drain_version_after"]
            or any(value is None for value in scheduled)
            or any(value is None for value in reopened)
        ):
            raise InvalidTaskTransition("Resolution replay projection is inconsistent")
        scheduled_by_subtask = {
            value.subtask_id: value
            for value in scheduled
            if value is not None
            and value.role is RunRole.EXECUTOR
            and value.status is RunStatus.QUEUED
        }
        if len(scheduled_by_subtask) != len(scheduled_ids) or not set(
            scheduled_by_subtask
        ).issubset(reopened_ids):
            raise InvalidTaskTransition("Resolution replay Subtask projection is inconsistent")
        for subtask in reopened:
            scheduled_run = None if subtask is None else scheduled_by_subtask.get(subtask.id)
            if (
                subtask is None
                or subtask.cancellation_source is not None
                or subtask.canceled_by_drain_id is not None
                or subtask.output is not None
                or subtask.error is not None
                or (
                    scheduled_run is not None
                    and (
                        subtask.status is not SubtaskStatus.READY
                        or subtask.current_run_id != scheduled_run.id
                    )
                )
                or (
                    scheduled_run is None
                    and (
                        subtask.status not in {SubtaskStatus.READY, SubtaskStatus.BLOCKED}
                        or subtask.current_run_id is not None
                    )
                )
            ):
                raise InvalidTaskTransition("Resolution replay Subtask projection is inconsistent")
        if any(value is None or value.subtask_id not in reopened_ids for value in scheduled):
            raise InvalidTaskTransition("Resolution replay Subtask projection is inconsistent")
        requested_by_run: dict[UUID, MessageEnvelope] = {}
        for message_id in run_requested_event_ids:
            requested = uow.outbox.get(message_id, tenant_id=task.tenant_id)
            if (
                requested is None
                or requested.schema_name != RUN_REQUESTED_SCHEMA
                or requested.schema_version != RUN_REQUESTED_VERSION
                or requested.producer != "agentmesh-control-api"
                or requested.correlation_id != task.id
                or requested.causation_id is not None
                or requested.idempotency_key != f"run:{requested.payload.get('run_id')}"
                or requested.payload.get("task_id") != str(task.id)
            ):
                raise InvalidTaskTransition("Resolution replay RunRequested Outbox is inconsistent")
            try:
                requested_run_id = UUID(str(requested.payload.get("run_id")))
            except (TypeError, ValueError) as exc:
                raise InvalidTaskTransition(
                    "Resolution replay RunRequested Outbox is inconsistent"
                ) from exc
            if requested_run_id in requested_by_run:
                raise InvalidTaskTransition("Resolution replay RunRequested Outbox is inconsistent")
            requested_by_run[requested_run_id] = requested
        if set(requested_by_run) != set(scheduled_ids):
            raise InvalidTaskTransition("Resolution replay RunRequested Outbox is inconsistent")
        resolution = uow.task_resolutions.get(resolution_id)
        expected_details = {
            "previous_budget": replay["previous_budget"],
            "replacement_budget": replay["replacement_budget"],
            "previous_budget_revision": replay["previous_budget_revision"],
            "budget_revision": replay["resulting_budget_revision"],
            "coordination_runtime_drain_id": str(drain.id),
            "drain_version_before": replay["drain_version_before"],
            "drain_version_after": replay["drain_version_after"],
            "reopened_subtask_ids": [str(value) for value in reopened_ids],
            "scheduled_run_ids": [str(value) for value in scheduled_ids],
        }
        if (
            resolution is None
            or resolution.task_id != task.id
            or resolution.action is not TaskResolutionAction.INCREASE_BUDGET_AND_RESUME
            or resolution.actor != actor.strip()
            or resolution.reason != reason.strip()
            or resolution.previous_status is not TaskStatus.WAITING_APPROVAL
            or resolution.resulting_status is not TaskStatus.RUNNING
            or resolution.previous_error != drain.reason
            or resolution.details != expected_details
        ):
            raise InvalidTaskTransition("Resolution replay audit is inconsistent")
        if any(value.occurred_at != resolution.created_at for value in requested_by_run.values()):
            raise InvalidTaskTransition("Resolution replay RunRequested Outbox is inconsistent")
        event = uow.outbox.get(event_id, tenant_id=task.tenant_id)
        if (
            event is None
            or event.to_dict()
            != self._resolution_event(
                task,
                resolution,
                message_id=event_id,
            ).to_dict()
        ):
            raise InvalidTaskTransition("Resolution replay Outbox is inconsistent")
        return TaskResolutionResult(resolution, self._aggregate(uow, task))

    @staticmethod
    def _validate_coordinated_budget_resume(
        aggregate: CoordinatedRuntimeAggregate,
    ) -> tuple[Any, tuple[Any, ...]]:
        task = aggregate.task
        drain = aggregate.active_drain
        run_ids = {value.id for value in aggregate.runs}
        if (
            task.status is not TaskStatus.WAITING_APPROVAL
            or task.current_run_id is not None
            or task.output is not None
            or task.candidate_output is not None
            or type(task.error) is not str
            or not task.error
            or task.budget_exhausted_reason != task.error
            or drain is None
            or drain.status is not CoordinationRuntimeDrainStatus.DRAINING
            or drain.target is not CoordinationRuntimeDrainTarget.WAITING_APPROVAL
            or drain.reason != task.error
            or set(aggregate.latest_attempts) != run_ids
            or set(aggregate.boundary_classifications) != run_ids
            or any(
                value is not CoordinationRuntimeBoundary.KNOWN_TERMINAL
                for value in aggregate.boundary_classifications.values()
            )
            or any(
                value.status in {RuntimeLifecycleStatus.REQUESTED, RuntimeLifecycleStatus.ACCEPTED}
                for value in aggregate.lifecycle_operations
            )
        ):
            raise InvalidTaskTransition("Managed coordinated budget resume projection is invalid")
        reopen = []
        for subtask in aggregate.subtasks:
            if subtask.status is not SubtaskStatus.CANCELED:
                continue
            if subtask.cancellation_source is SubtaskCancellationSource.BUDGET_DRAIN:
                if subtask.canceled_by_drain_id != drain.id:
                    raise InvalidTaskTransition(
                        "Budget-canceled Subtask references a different drain"
                    )
                owners = tuple(
                    run
                    for run in aggregate.runs
                    if run.id == subtask.current_run_id
                    and run.role is RunRole.EXECUTOR
                    and run.subtask_id == subtask.id
                )
                if len(owners) != 1:
                    raise InvalidTaskTransition(
                        "Budget-canceled Subtask Executor lineage is invalid"
                    )
                reopen.append(subtask)
        return drain, tuple(sorted(reopen, key=lambda value: value.id))

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
            return self._queue_replacement(
                uow, task, latest, RunRole.EXECUTOR, kind=ContinuationKind.REPLACEMENT
            )
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
            return self._queue_replacement(
                uow, task, latest, role, kind=ContinuationKind.REPLACEMENT
            )
        if latest.role == RunRole.EXECUTOR and latest.status == RunStatus.SUCCEEDED:
            return self._queue_replacement(
                uow, task, latest, RunRole.REVIEWER, kind=ContinuationKind.REVIEWER
            )
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
        run = self._authority_cohort_resolver.create_continuation_in_uow(
            uow,
            task,
            agent_id=executor_name,
            agent_version_id=executor_version.id,
            agent_version_digest=executor_version.content_digest,
            role=RunRole.EXECUTOR,
            revision_number=task.revision_count + 1,
            parent_run=latest,
            kind=ContinuationKind.REVISION,
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
        *,
        kind: ContinuationKind,
    ) -> TaskRun:
        self._require_future_admission(uow, task)
        if previous is not None:
            run = self._authority_cohort_resolver.create_continuation_in_uow(
                uow,
                task,
                agent_id=previous.agent_id,
                agent_version_id=previous.agent_version_id,
                agent_version_digest=previous.agent_version_digest,
                role=role,
                revision_number=previous.revision_number,
                parent_run=previous,
                kind=kind,
            )
        else:
            configured = (
                self._reviewer_agent_id if role == RunRole.REVIEWER else self._executor_agent_id
            )
            agent_name, agent_version = TaskApplicationService._resolve_agent_by_name(
                uow, task.tenant_id, configured
            )
            run = self._authority_cohort_resolver.create_initial_in_uow(
                uow,
                task,
                agent_id=agent_name,
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
        if type(existing.result) is not dict:
            raise InvalidTaskTransition("Resolution idempotency projection is invalid")
        return dict(existing.result)
