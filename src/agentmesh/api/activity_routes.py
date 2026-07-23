from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field

from agentmesh.api.feature_routes import require_feature
from agentmesh.api.security import PrincipalDependency, require_permission
from agentmesh.application.activity_services import (
    ActivityEvent,
    InteractionEndpoint,
    InteractionEvent,
    TaskActivityService,
)
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
    next_cursor: str | None = None


class InteractionEndpointResponse(BaseModel):
    type: str
    id: str
    label: str | None

    @classmethod
    def from_domain(cls, endpoint: InteractionEndpoint) -> InteractionEndpointResponse:
        return cls(type=endpoint.type, id=endpoint.id, label=endpoint.label)


class InteractionEventResponse(BaseModel):
    id: str
    occurred_at: datetime
    kind: str
    source: InteractionEndpointResponse
    target: InteractionEndpointResponse
    transport: str
    payload_kind: str
    status: str
    trace_id: str | None
    summary: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_domain(cls, event: InteractionEvent) -> InteractionEventResponse:
        return cls(
            id=event.id,
            occurred_at=event.occurred_at,
            kind=event.kind,
            source=InteractionEndpointResponse.from_domain(event.source),
            target=InteractionEndpointResponse.from_domain(event.target),
            transport=event.transport,
            payload_kind=event.payload_kind,
            status=event.status,
            trace_id=event.trace_id,
            summary=dict(event.summary or {}),
        )


class InteractionTimelineResponse(BaseModel):
    task_id: UUID
    items: list[InteractionEventResponse]
    limit: int
    next_cursor: str | None = None


class ReplayBookmarkCreateRequest(BaseModel):
    event_id: str = Field(min_length=1, max_length=255)
    label: str = Field(min_length=1, max_length=120)


class ReplayBookmarkResponse(BaseModel):
    id: UUID
    task_id: UUID
    event_id: str
    label: str
    created_by: str
    created_at: datetime


def get_activity_service(request: Request) -> TaskActivityService:
    return request.app.state.container.activity_service


ActivityServiceDependency = Annotated[TaskActivityService, Depends(get_activity_service)]
LimitQuery = Annotated[int, Query(ge=1, le=200)]
CursorQuery = Annotated[str | None, Query(max_length=1024)]


def _encode_cursor(*, projection: str, event_id: str) -> str:
    raw = json.dumps({"v": 1, "p": projection, "id": event_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _page(items: list[Any], *, cursor: str | None, limit: int, projection: str):
    start = 0
    if cursor:
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            value = json.loads(base64.urlsafe_b64decode(padded).decode())
            if value.get("v") != 1 or value.get("p") != projection:
                raise ValueError
            start = next(index + 1 for index, item in enumerate(items) if item.id == value["id"])
        except (ValueError, KeyError, StopIteration, json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Invalid or expired activity cursor",
            ) from None
    page = items[start : start + limit]
    next_cursor = (
        _encode_cursor(projection=projection, event_id=page[-1].id)
        if page and start + limit < len(items)
        else None
    )
    return page, next_cursor


@router.get(
    "/tasks/{task_id}/activity",
    response_model=ActivityTimelineResponse,
    response_model_exclude_none=True,
)
def get_task_activity(
    task_id: UUID,
    service: ActivityServiceDependency,
    limit: LimitQuery = 100,
    cursor: CursorQuery = None,
) -> ActivityTimelineResponse:
    events, next_cursor = _page(
        service.timeline(task_id, limit=10_000),
        cursor=cursor,
        limit=limit,
        projection="activity",
    )
    return ActivityTimelineResponse(
        task_id=task_id,
        items=[ActivityEventResponse.from_domain(event) for event in events],
        limit=limit,
        next_cursor=next_cursor,
    )


@router.get(
    "/tasks/{task_id}/interactions",
    response_model=InteractionTimelineResponse,
    response_model_exclude_none=True,
)
def get_task_interactions(
    task_id: UUID,
    service: ActivityServiceDependency,
    limit: LimitQuery = 100,
    cursor: CursorQuery = None,
) -> InteractionTimelineResponse:
    events, next_cursor = _page(
        service.interactions(task_id, limit=10_000),
        cursor=cursor,
        limit=limit,
        projection="interactions",
    )
    return InteractionTimelineResponse(
        task_id=task_id,
        items=[InteractionEventResponse.from_domain(event) for event in events],
        limit=limit,
        next_cursor=next_cursor,
    )


@router.get(
    "/tasks/{task_id}/replay-bookmarks",
    response_model=list[ReplayBookmarkResponse],
)
def list_replay_bookmarks(
    task_id: UUID, service: ActivityServiceDependency
) -> list[ReplayBookmarkResponse]:
    return [
        ReplayBookmarkResponse.model_validate(item, from_attributes=True)
        for item in service.list_bookmarks(task_id)
    ]


@router.post(
    "/tasks/{task_id}/replay-bookmarks",
    response_model=ReplayBookmarkResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(Permission.TASK_OPERATE))],
)
def create_replay_bookmark(
    task_id: UUID,
    body: ReplayBookmarkCreateRequest,
    service: ActivityServiceDependency,
    principal: PrincipalDependency,
) -> ReplayBookmarkResponse:
    try:
        bookmark = service.create_bookmark(
            task_id,
            event_id=body.event_id,
            label=body.label,
            created_by=principal.principal_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return ReplayBookmarkResponse.model_validate(bookmark, from_attributes=True)


@router.delete(
    "/tasks/{task_id}/replay-bookmarks/{bookmark_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission(Permission.TASK_OPERATE))],
)
def delete_replay_bookmark(
    task_id: UUID, bookmark_id: UUID, service: ActivityServiceDependency
) -> Response:
    if not service.delete_bookmark(task_id, bookmark_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Replay bookmark not found"
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
