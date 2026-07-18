from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from agentmesh.api.feature_routes import require_feature
from agentmesh.api.security import require_permission
from agentmesh.application.tool_services import ToolInvocationService
from agentmesh.domain.identity import Permission
from agentmesh.domain.tools import ToolInvocation, ToolInvocationStatus, ToolSideEffect
from agentmesh.features import Feature

router = APIRouter(
    prefix="/api/v1",
    tags=["mcp"],
    dependencies=[
        Depends(require_permission(Permission.TOOL_AUDIT_READ)),
        Depends(require_feature(Feature.MCP_READ_TOOLS)),
    ],
)


class ToolInvocationResponse(BaseModel):
    id: UUID
    tenant_id: str
    task_id: UUID
    run_id: UUID
    server_name: str
    tool_key: str
    tool_name: str
    side_effect: ToolSideEffect
    protocol_version: str | None
    schema_digest: str | None
    arguments_digest: str
    status: ToolInvocationStatus
    result_digest: str | None
    result_bytes: int | None
    error: str | None
    started_at: datetime
    completed_at: datetime | None

    @classmethod
    def from_domain(cls, value: ToolInvocation) -> ToolInvocationResponse:
        return cls(
            id=value.id,
            tenant_id=value.tenant_id,
            task_id=value.task_id,
            run_id=value.run_id,
            server_name=value.server_name,
            tool_key=value.tool_key,
            tool_name=value.tool_name,
            side_effect=value.side_effect,
            protocol_version=value.protocol_version,
            schema_digest=value.schema_digest,
            arguments_digest=value.arguments_digest,
            status=value.status,
            result_digest=value.result_digest,
            result_bytes=value.result_bytes,
            error=value.error,
            started_at=value.started_at,
            completed_at=value.completed_at,
        )


class ToolInvocationListResponse(BaseModel):
    items: list[ToolInvocationResponse]


def get_tool_invocation_service(request: Request) -> ToolInvocationService:
    return request.app.state.container.tool_invocation_service


ToolInvocationServiceDependency = Annotated[
    ToolInvocationService,
    Depends(get_tool_invocation_service),
]


@router.get(
    "/tasks/{task_id}/tool-invocations",
    response_model=ToolInvocationListResponse,
)
def list_task_tool_invocations(
    task_id: UUID,
    service: ToolInvocationServiceDependency,
) -> ToolInvocationListResponse:
    return ToolInvocationListResponse(
        items=[
            ToolInvocationResponse.from_domain(value) for value in service.list_for_task(task_id)
        ]
    )
