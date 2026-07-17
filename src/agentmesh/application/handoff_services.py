from __future__ import annotations

import json
from hashlib import sha256
from typing import Any
from uuid import UUID

from agentmesh.application.coordination_services import CoordinatedScheduler
from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.domain.coordination import SubtaskStatus
from agentmesh.domain.errors import (
    HandoffNotFound,
    IdempotencyConflict,
    InvalidTaskInput,
    InvalidTaskTransition,
    TaskNotFound,
)
from agentmesh.domain.handoffs import Handoff, HandoffStatus
from agentmesh.domain.messaging import IdempotencyRecord, MessageEnvelope
from agentmesh.domain.tasks import AttemptStatus, RunStatus, TaskExecutionMode, TaskStatus
from agentmesh.features import Feature, FeatureGateSet


class HandoffApplicationService:
    MAX_HANDOFFS_PER_TASK = 8

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        tenant_id: str,
        supervisor_agent_id: str,
        feature_gates: FeatureGateSet,
    ) -> None:
        self._uow_factory = uow_factory
        self._tenant_id = tenant_id
        self._scheduler = CoordinatedScheduler(supervisor_agent_id=supervisor_agent_id)
        self._feature_gates = feature_gates

    def request_handoff(
        self,
        *,
        task_id: UUID,
        source_subtask_id: UUID,
        target_subtask_id: UUID,
        target_agent_id: str,
        objective: str,
        reason: str,
        completed_work_summary: str,
        requested_by: str,
        unresolved_questions: tuple[str, ...] = (),
        constraints: dict[str, Any] | None = None,
        acceptance_criteria: tuple[dict[str, Any], ...] = (),
        idempotency_key: str | None = None,
    ) -> Handoff:
        self._feature_gates.require(Feature.HANDOFFS)
        request = {
            "task_id": str(task_id),
            "source_subtask_id": str(source_subtask_id),
            "target_subtask_id": str(target_subtask_id),
            "target_agent_id": target_agent_id,
            "objective": objective,
            "reason": reason,
            "completed_work_summary": completed_work_summary,
            "requested_by": requested_by,
            "unresolved_questions": list(unresolved_questions),
            "constraints": dict(constraints or {}),
            "acceptance_criteria": [dict(value) for value in acceptance_criteria],
        }
        scope = f"handoff-request:{self._tenant_id}:{task_id}"
        request_hash = self._request_hash(request)
        normalized_key = (idempotency_key or "").strip()
        if idempotency_key is not None and not normalized_key:
            raise InvalidTaskInput("Idempotency-Key must not be blank")
        with self._uow_factory() as uow:
            replay = self._idempotent_replay(uow, scope, normalized_key, request_hash)
            if replay is not None:
                return self._get_handoff_or_raise(uow, UUID(replay["handoff_id"]))
            task = self._get_task_or_raise(uow, task_id)
            self._require_coordinated_task(task)
            source = uow.subtasks.get(source_subtask_id, for_update=True)
            target = uow.subtasks.get(target_subtask_id, for_update=True)
            if (
                source is None
                or target is None
                or source.task_id != task.id
                or target.task_id != task.id
            ):
                raise InvalidTaskInput("Handoff Subtasks must belong to the Task")
            if source.status != SubtaskStatus.COMPLETED or source.current_run_id is None:
                raise InvalidTaskTransition("Handoff source Subtask must be completed")
            source_run = uow.runs.get(source.current_run_id, for_update=True)
            if source_run is None or source_run.status != RunStatus.SUCCEEDED:
                raise InvalidTaskTransition("Handoff source Run must have succeeded")
            source_attempt = uow.attempts.latest_for_run(source_run.id)
            if source_attempt is None or source_attempt.status != AttemptStatus.SUCCEEDED:
                raise InvalidTaskTransition("Handoff source Attempt must have succeeded")
            if requested_by.strip().lower() != source_run.agent_id:
                raise InvalidTaskInput("Handoff requester must be the source Run Agent")
            self._require_target_unstarted(target)
            dependencies = uow.subtask_dependencies.list_for_task(task.id)
            if not self._is_ancestor(source.id, target.id, dependencies):
                raise InvalidTaskInput(
                    "Handoff target must be a downstream Subtask of the source"
                )
            if len(uow.handoffs.list_for_task(task.id)) >= self.MAX_HANDOFFS_PER_TASK:
                raise InvalidTaskInput(
                    f"A Task supports at most {self.MAX_HANDOFFS_PER_TASK} Handoffs"
                )
            CoordinatedScheduler.resolve_named_agent(
                uow,
                task.tenant_id,
                target_agent_id,
                set(target.required_capabilities),
            )
            handoff = Handoff.request(
                task_id=task.id,
                source_subtask_id=source.id,
                source_run_id=source_run.id,
                source_trace_id=source_attempt.trace_id,
                source_agent_id=source_run.agent_id,
                target_subtask_id=target.id,
                target_agent_id=target_agent_id,
                objective=objective,
                reason=reason,
                completed_work_summary=completed_work_summary,
                unresolved_questions=unresolved_questions,
                constraints=constraints,
                acceptance_criteria=acceptance_criteria,
                requested_by=requested_by,
            )
            uow.handoffs.add(handoff)
            uow.outbox.add(self._event(task, handoff, "requested"))
            self._store_idempotency(
                uow, scope, normalized_key, request_hash, {"handoff_id": str(handoff.id)}
            )
            uow.commit()
            return handoff

    def accept_handoff(
        self,
        task_id: UUID,
        handoff_id: UUID,
        *,
        actor: str,
        reason: str | None = None,
        idempotency_key: str | None = None,
    ) -> Handoff:
        return self._decide(
            task_id,
            handoff_id,
            accepted=True,
            actor=actor,
            reason=reason,
            idempotency_key=idempotency_key,
        )

    def reject_handoff(
        self,
        task_id: UUID,
        handoff_id: UUID,
        *,
        actor: str,
        reason: str,
        idempotency_key: str | None = None,
    ) -> Handoff:
        return self._decide(
            task_id,
            handoff_id,
            accepted=False,
            actor=actor,
            reason=reason,
            idempotency_key=idempotency_key,
        )

    def get_handoff(self, task_id: UUID, handoff_id: UUID) -> Handoff:
        self._feature_gates.require(Feature.HANDOFFS)
        with self._uow_factory() as uow:
            task = self._get_task_or_raise(uow, task_id)
            handoff = self._get_handoff_or_raise(uow, handoff_id)
            if handoff.task_id != task.id:
                raise HandoffNotFound(handoff_id)
            return handoff

    def _decide(
        self,
        task_id: UUID,
        handoff_id: UUID,
        *,
        accepted: bool,
        actor: str,
        reason: str | None,
        idempotency_key: str | None,
    ) -> Handoff:
        self._feature_gates.require(Feature.HANDOFFS)
        action = "accept" if accepted else "reject"
        request = {"handoff_id": str(handoff_id), "actor": actor, "reason": reason}
        scope = f"handoff-{action}:{self._tenant_id}:{task_id}"
        request_hash = self._request_hash(request)
        normalized_key = (idempotency_key or "").strip()
        if idempotency_key is not None and not normalized_key:
            raise InvalidTaskInput("Idempotency-Key must not be blank")
        with self._uow_factory() as uow:
            replay = self._idempotent_replay(uow, scope, normalized_key, request_hash)
            if replay is not None:
                return self._get_handoff_or_raise(uow, UUID(replay["handoff_id"]))
            task = self._get_task_or_raise(uow, task_id)
            self._require_coordinated_task(task)
            handoff = self._get_handoff_or_raise(uow, handoff_id, for_update=True)
            if handoff.task_id != task.id:
                raise HandoffNotFound(handoff_id)
            if actor.strip().lower() != handoff.target_agent_id:
                raise InvalidTaskInput("Handoff decision actor must be the target Agent")
            if handoff.status != HandoffStatus.REQUESTED:
                if accepted:
                    handoff.accept(actor=actor, reason=reason)
                else:
                    handoff.reject(actor=actor, reason=reason or "")
                self._store_idempotency(
                    uow,
                    scope,
                    normalized_key,
                    request_hash,
                    {"handoff_id": str(handoff.id)},
                )
                uow.commit()
                return handoff
            target = uow.subtasks.get(handoff.target_subtask_id, for_update=True)
            if target is None or target.task_id != task.id:
                raise InvalidTaskInput("Handoff target Subtask no longer exists")
            self._require_target_unstarted(target)
            if accepted:
                existing = uow.handoffs.list_for_target(
                    target.id, status=HandoffStatus.ACCEPTED
                )
                if existing and existing[0].id != handoff.id:
                    raise InvalidTaskTransition(
                        "Target Subtask already has an accepted Handoff"
                    )
                CoordinatedScheduler.resolve_named_agent(
                    uow,
                    task.tenant_id,
                    handoff.target_agent_id,
                    set(target.required_capabilities),
                )
                handoff.accept(actor=actor, reason=reason)
            else:
                handoff.reject(actor=actor, reason=reason or "")
            uow.handoffs.save(handoff)
            uow.outbox.add(self._event(task, handoff, action + "ed"))
            if accepted:
                self._scheduler.schedule(uow, task)
                uow.tasks.save(task)
            self._store_idempotency(
                uow, scope, normalized_key, request_hash, {"handoff_id": str(handoff.id)}
            )
            uow.commit()
            return handoff

    def _get_task_or_raise(self, uow: Any, task_id: UUID):
        task = uow.tasks.get(task_id, for_update=True)
        if task is None or task.tenant_id != self._tenant_id:
            raise TaskNotFound(task_id)
        return task

    @staticmethod
    def _get_handoff_or_raise(
        uow: Any, handoff_id: UUID, *, for_update: bool = False
    ) -> Handoff:
        handoff = uow.handoffs.get(handoff_id, for_update=for_update)
        if handoff is None:
            raise HandoffNotFound(handoff_id)
        return handoff

    @staticmethod
    def _require_coordinated_task(task: Any) -> None:
        if (
            task.execution_mode != TaskExecutionMode.COORDINATED
            or task.status != TaskStatus.RUNNING
        ):
            raise InvalidTaskTransition("Handoffs require a running coordinated Task")

    @staticmethod
    def _require_target_unstarted(target: Any) -> None:
        if (
            target.status not in {SubtaskStatus.BLOCKED, SubtaskStatus.READY}
            or target.current_run_id is not None
        ):
            raise InvalidTaskTransition("Handoff target Subtask must not have started")

    @staticmethod
    def _is_ancestor(source_id: UUID, target_id: UUID, dependencies: list[Any]) -> bool:
        successors: dict[UUID, set[UUID]] = {}
        for dependency in dependencies:
            successors.setdefault(dependency.predecessor_id, set()).add(
                dependency.successor_id
            )
        pending = list(successors.get(source_id, set()))
        visited: set[UUID] = set()
        while pending:
            value = pending.pop()
            if value == target_id:
                return True
            if value in visited:
                continue
            visited.add(value)
            pending.extend(successors.get(value, set()))
        return False

    @staticmethod
    def _event(task: Any, handoff: Handoff, action: str) -> MessageEnvelope:
        return MessageEnvelope.domain_event(
            schema_name=f"agentmesh.handoff.{action}",
            tenant_id=task.tenant_id,
            aggregate_id=task.id,
            causation_id=handoff.causation_id,
            payload={
                "task_id": str(task.id),
                "handoff_id": str(handoff.id),
                "source_subtask_id": str(handoff.source_subtask_id),
                "target_subtask_id": str(handoff.target_subtask_id),
                "source_agent_id": handoff.source_agent_id,
                "target_agent_id": handoff.target_agent_id,
                "status": handoff.status.value,
                "actor": handoff.decided_by or handoff.requested_by,
            },
        )

    @staticmethod
    def _request_hash(value: dict[str, Any]) -> str:
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
        return sha256(canonical.encode()).hexdigest()

    @staticmethod
    def _idempotent_replay(
        uow: Any, scope: str, key: str, request_hash: str
    ) -> dict[str, Any] | None:
        if not key:
            return None
        uow.idempotency.lock(scope, key)
        record = uow.idempotency.get(scope, key)
        if record is None:
            return None
        if record.request_hash != request_hash:
            raise IdempotencyConflict(
                f"Idempotency key '{key}' was already used with a different request"
            )
        return dict(record.result)

    @staticmethod
    def _store_idempotency(
        uow: Any,
        scope: str,
        key: str,
        request_hash: str,
        result: dict[str, Any],
    ) -> None:
        if key:
            uow.idempotency.add(
                IdempotencyRecord.create(
                    scope=scope,
                    key=key,
                    request_hash=request_hash,
                    result=result,
                )
            )
