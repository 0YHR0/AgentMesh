from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from agentmesh.api.feature_routes import require_feature
from agentmesh.api.security import require_permission
from agentmesh.application.activity_services import ActivityEvent, TaskActivityService
from agentmesh.domain.identity import Permission
from agentmesh.features import Feature

router = APIRouter(
    prefix="/api/v1",
    tags=["activity"],
    dependencies=[
        Depends(require_feature(Feature.ACTIVITY_TIMELINE)),
        Depends(require_permission(Permission.TASK_READ)),
    ],
)


class ActivityEventResponse(BaseModel):
    id: str
    category: str
    action: str
    status: str
    title: str
    occurred_at: datetime
    entity_type: str
    entity_id: str
    actor: str | None
    trace_id: str | None
    details: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_domain(cls, event: ActivityEvent) -> ActivityEventResponse:
        return cls(
            id=event.id,
            category=event.category,
            action=event.action,
            status=event.status,
            title=event.title,
            occurred_at=event.occurred_at,
            entity_type=event.entity_type,
            entity_id=event.entity_id,
            actor=event.actor,
            trace_id=event.trace_id,
            details=dict(event.details or {}),
        )


class ActivityTimelineResponse(BaseModel):
    task_id: UUID
    items: list[ActivityEventResponse]
    limit: int


def get_activity_service(request: Request) -> TaskActivityService:
    return request.app.state.container.activity_service


ActivityServiceDependency = Annotated[TaskActivityService, Depends(get_activity_service)]
LimitQuery = Annotated[int, Query(ge=1, le=200)]


@router.get("/tasks/{task_id}/activity", response_model=ActivityTimelineResponse)
def get_task_activity(
    task_id: UUID,
    service: ActivityServiceDependency,
    limit: LimitQuery = 100,
) -> ActivityTimelineResponse:
    events = service.timeline(task_id, limit=limit)
    return ActivityTimelineResponse(
        task_id=task_id,
        items=[ActivityEventResponse.from_domain(event) for event in events],
        limit=limit,
    )
