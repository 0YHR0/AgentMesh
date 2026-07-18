from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from agentmesh.domain.errors import InvalidTaskInput
from agentmesh.domain.tasks import TaskStatus, utc_now


class TaskResolutionAction(str, Enum):
    ACCEPT_CANDIDATE = "ACCEPT_CANDIDATE"
    REJECT_TASK = "REJECT_TASK"
    INCREASE_BUDGET_AND_RESUME = "INCREASE_BUDGET_AND_RESUME"


@dataclass(frozen=True)
class TaskResolution:
    id: UUID
    task_id: UUID
    action: TaskResolutionAction
    actor: str
    reason: str
    previous_status: TaskStatus
    resulting_status: TaskStatus
    previous_error: str | None
    details: dict[str, Any]
    created_at: datetime

    @classmethod
    def create(
        cls,
        *,
        task_id: UUID,
        action: TaskResolutionAction,
        actor: str,
        reason: str,
        previous_status: TaskStatus,
        resulting_status: TaskStatus,
        previous_error: str | None,
        details: dict[str, Any] | None = None,
    ) -> TaskResolution:
        normalized_actor = actor.strip()
        normalized_reason = reason.strip()
        if not normalized_actor or len(normalized_actor) > 128:
            raise InvalidTaskInput("Resolution actor must contain 1 to 128 characters")
        if not normalized_reason or len(normalized_reason) > 2_000:
            raise InvalidTaskInput("Resolution reason must contain 1 to 2000 characters")
        normalized_details = dict(details or {})
        if len(normalized_details) > 32:
            raise InvalidTaskInput("Resolution details support at most 32 fields")
        return cls(
            id=uuid4(),
            task_id=task_id,
            action=action,
            actor=normalized_actor,
            reason=normalized_reason,
            previous_status=previous_status,
            resulting_status=resulting_status,
            previous_error=previous_error,
            details=normalized_details,
            created_at=utc_now(),
        )
