from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from agentmesh.domain.coordination import normalize_agent_name
from agentmesh.domain.errors import InvalidTaskInput, InvalidTaskTransition


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class HandoffStatus(str, Enum):
    REQUESTED = "REQUESTED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


@dataclass
class Handoff:
    id: UUID
    task_id: UUID
    source_subtask_id: UUID
    source_run_id: UUID
    source_trace_id: str
    causation_id: UUID
    source_agent_id: str
    target_subtask_id: UUID
    target_agent_id: str
    objective: str
    reason: str
    completed_work_summary: str
    unresolved_questions: tuple[str, ...]
    constraints: dict[str, Any]
    acceptance_criteria: tuple[dict[str, Any], ...]
    status: HandoffStatus
    requested_by: str
    requested_at: datetime
    decided_by: str | None
    decision_reason: str | None
    decided_at: datetime | None
    version: int

    @classmethod
    def request(
        cls,
        *,
        task_id: UUID,
        source_subtask_id: UUID,
        source_run_id: UUID,
        source_trace_id: str,
        source_agent_id: str,
        target_subtask_id: UUID,
        target_agent_id: str,
        objective: str,
        reason: str,
        completed_work_summary: str,
        unresolved_questions: tuple[str, ...] | list[str] = (),
        constraints: dict[str, Any] | None = None,
        acceptance_criteria: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
        requested_by: str,
        causation_id: UUID | None = None,
    ) -> Handoff:
        if source_subtask_id == target_subtask_id:
            raise InvalidTaskInput("Handoff source and target Subtasks must be distinct")
        normalized_objective = cls._required_text(objective, "objective", 20_000)
        normalized_reason = cls._required_text(reason, "reason", 2_000)
        summary = cls._required_text(completed_work_summary, "completed work summary", 20_000)
        requester = normalize_agent_name(requested_by)
        source_agent = normalize_agent_name(source_agent_id)
        target_agent = normalize_agent_name(target_agent_id)
        normalized_trace_id = source_trace_id.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{32}", normalized_trace_id):
            raise InvalidTaskInput("Handoff source Trace ID must contain 32 hex characters")
        if source_agent == target_agent:
            raise InvalidTaskInput("Handoff source and target Agents must be distinct")
        questions = tuple(
            cls._required_text(value, "unresolved question", 2_000)
            for value in unresolved_questions
        )
        if len(questions) > 20:
            raise InvalidTaskInput("Handoff supports at most 20 unresolved questions")
        raw_criteria = tuple(dict(value) for value in acceptance_criteria)
        if len(raw_criteria) > 20:
            raise InvalidTaskInput("Handoff supports at most 20 acceptance criteria")
        criteria: list[dict[str, Any]] = []
        criterion_keys: set[str] = set()
        for value in raw_criteria:
            key = cls._required_text(str(value.get("key", "")), "criterion key", 128)
            description = cls._required_text(
                str(value.get("description", "")), "criterion description", 2_000
            )
            if key in criterion_keys:
                raise InvalidTaskInput("Handoff acceptance criterion keys must be unique")
            criterion_keys.add(key)
            criteria.append(
                {
                    **value,
                    "key": key,
                    "description": description,
                    "required": bool(value.get("required", True)),
                }
            )
        now = utc_now()
        return cls(
            id=uuid4(),
            task_id=task_id,
            source_subtask_id=source_subtask_id,
            source_run_id=source_run_id,
            source_trace_id=normalized_trace_id,
            causation_id=causation_id or uuid4(),
            source_agent_id=source_agent,
            target_subtask_id=target_subtask_id,
            target_agent_id=target_agent,
            objective=normalized_objective,
            reason=normalized_reason,
            completed_work_summary=summary,
            unresolved_questions=questions,
            constraints=dict(constraints or {}),
            acceptance_criteria=tuple(criteria),
            status=HandoffStatus.REQUESTED,
            requested_by=requester,
            requested_at=now,
            decided_by=None,
            decision_reason=None,
            decided_at=None,
            version=1,
        )

    def accept(self, *, actor: str, reason: str | None = None) -> bool:
        return self._decide(HandoffStatus.ACCEPTED, actor=actor, reason=reason)

    def reject(self, *, actor: str, reason: str) -> bool:
        return self._decide(HandoffStatus.REJECTED, actor=actor, reason=reason)

    def _decide(
        self,
        status: HandoffStatus,
        *,
        actor: str,
        reason: str | None,
    ) -> bool:
        normalized_actor = normalize_agent_name(actor)
        normalized_reason = reason.strip() if reason is not None else None
        if status == HandoffStatus.REJECTED and not normalized_reason:
            raise InvalidTaskInput("Handoff rejection requires a reason")
        if self.status == status:
            if self.decided_by == normalized_actor and self.decision_reason == normalized_reason:
                return False
            raise InvalidTaskTransition(
                "Handoff decision payload conflicts with its terminal state"
            )
        if self.status != HandoffStatus.REQUESTED:
            raise InvalidTaskTransition(
                f"Cannot {status.value.lower()} Handoff {self.id} from {self.status.value}"
            )
        self.status = status
        self.decided_by = normalized_actor
        self.decision_reason = normalized_reason
        self.decided_at = utc_now()
        self.version += 1
        return True

    def execution_context(self) -> dict[str, Any]:
        if self.status != HandoffStatus.ACCEPTED:
            raise InvalidTaskTransition("Only accepted Handoffs can enter execution context")
        return {
            "handoff_id": str(self.id),
            "source_subtask_id": str(self.source_subtask_id),
            "source_run_id": str(self.source_run_id),
            "source_trace_id": self.source_trace_id,
            "causation_id": str(self.causation_id),
            "source_agent_id": self.source_agent_id,
            "target_agent_id": self.target_agent_id,
            "objective": self.objective,
            "reason": self.reason,
            "completed_work_summary": self.completed_work_summary,
            "unresolved_questions": list(self.unresolved_questions),
            "constraints": dict(self.constraints),
            "acceptance_criteria": [dict(value) for value in self.acceptance_criteria],
        }

    @staticmethod
    def _required_text(value: str, field: str, max_length: int) -> str:
        normalized = value.strip()
        if not normalized:
            raise InvalidTaskInput(f"Handoff {field} must not be empty")
        if len(normalized) > max_length:
            raise InvalidTaskInput(f"Handoff {field} must contain at most {max_length} characters")
        return normalized
