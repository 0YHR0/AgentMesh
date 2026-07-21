from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, status
from pydantic import BaseModel, Field

from agentmesh.api.feature_routes import require_feature
from agentmesh.api.policy_routes import GovernedActionResponse
from agentmesh.api.security import PrincipalDependency, require_permission
from agentmesh.application.mcp_registry_services import McpRegistryService, McpServerView
from agentmesh.application.tool_services import ToolInvocationService
from agentmesh.domain.identity import Permission
from agentmesh.domain.mcp_registry import (
    McpDiscoveredTool,
    McpDiscoverySnapshot,
    McpDiscoveryStatus,
    McpServerStatus,
    McpServerVersion,
    McpServerVersionStatus,
    McpToolCapability,
    McpTransport,
)
from agentmesh.domain.tools import (
    ToolAuthorizationStatus,
    ToolCallRequest,
    ToolExecutionAuthorization,
    ToolInvocation,
    ToolInvocationStatus,
    ToolSideEffect,
)
from agentmesh.features import Feature

router = APIRouter(
    prefix="/api/v1",
    tags=["mcp"],
    dependencies=[
        Depends(require_permission(Permission.TOOL_AUDIT_READ)),
        Depends(require_feature(Feature.MCP_READ_TOOLS)),
    ],
)

registry_router = APIRouter(
    prefix="/api/v1/mcp",
    tags=["mcp-registry"],
    dependencies=[Depends(require_feature(Feature.GOVERNED_MCP))],
)

IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)]
ExecutionPermitId = Annotated[UUID | None, Header(alias="Execution-Permit-Id")]


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


class ToolExecutionAuthorizationResponse(BaseModel):
    id: UUID
    governed_action_id: UUID
    principal_id: str
    server_id: UUID
    server_version_id: UUID
    configuration_digest: str
    tool_key: str
    tool_name: str
    side_effect: ToolSideEffect
    schema_digest: str
    arguments_digest: str
    idempotency_key_digest: str
    status: ToolAuthorizationStatus
    invocation_id: UUID | None
    created_at: datetime
    completed_at: datetime | None

    @classmethod
    def from_domain(
        cls, value: ToolExecutionAuthorization
    ) -> ToolExecutionAuthorizationResponse:
        return cls(
            id=value.id,
            governed_action_id=value.governed_action_id,
            principal_id=value.principal_id,
            server_id=value.server_id,
            server_version_id=value.server_version_id,
            configuration_digest=value.configuration_digest,
            tool_key=value.tool_key,
            tool_name=value.tool_name,
            side_effect=value.side_effect,
            schema_digest=value.schema_digest,
            arguments_digest=value.arguments_digest,
            idempotency_key_digest=value.idempotency_key_digest,
            status=value.status,
            invocation_id=value.invocation_id,
            created_at=value.created_at,
            completed_at=value.completed_at,
        )


class ToolInvocationListResponse(BaseModel):
    items: list[ToolInvocationResponse]
    authorization: ToolExecutionAuthorizationResponse | None


def get_tool_invocation_service(request: Request) -> ToolInvocationService:
    return request.app.state.container.tool_invocation_service


ToolInvocationServiceDependency = Annotated[
    ToolInvocationService,
    Depends(get_tool_invocation_service),
]


def get_mcp_registry_service(request: Request) -> McpRegistryService:
    return request.app.state.container.mcp_registry_service


McpRegistryServiceDependency = Annotated[
    McpRegistryService,
    Depends(get_mcp_registry_service),
]


@router.get(
    "/tasks/{task_id}/tool-invocations",
    response_model=ToolInvocationListResponse,
)
def list_task_tool_invocations(
    task_id: UUID,
    service: ToolInvocationServiceDependency,
) -> ToolInvocationListResponse:
    authorization, invocations = service.audit_for_task(task_id)
    return ToolInvocationListResponse(
        items=[ToolInvocationResponse.from_domain(value) for value in invocations],
        authorization=(
            ToolExecutionAuthorizationResponse.from_domain(authorization)
            if authorization is not None
            else None
        ),
    )


class CreateMcpServerRequest(BaseModel):
    owner_id: str
    name: str
    description: str = ""
    transport: McpTransport
    endpoint_reference: str
    authentication_required: bool = False


class CreateMcpVersionRequest(BaseModel):
    semantic_version: str
    protocol_version: str = "2025-11-25"
    configuration: dict


class CreateMcpToolRequest(BaseModel):
    logical_key: str
    tool_name: str
    description: str = ""
    side_effect: ToolSideEffect
    input_schema: dict


class RequestMcpToolExecutionIntent(BaseModel):
    tool_key: str = Field(min_length=1, max_length=255)
    arguments: dict


@registry_router.post(
    "/tool-execution-intents",
    response_model=GovernedActionResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        Depends(require_feature(Feature.MCP_WRITE_TOOLS)),
        Depends(require_permission(Permission.POLICY_REQUEST)),
    ],
)
def request_mcp_tool_execution_intent(
    payload: RequestMcpToolExecutionIntent,
    principal: PrincipalDependency,
    service: McpRegistryServiceDependency,
    idempotency_key: IdempotencyKey,
) -> GovernedActionResponse:
    request = ToolCallRequest.from_task_input(
        {"tool_call": {"tool": payload.tool_key, "arguments": payload.arguments}}
    )
    assert request is not None
    return GovernedActionResponse.from_result(
        service.request_write_action(
            principal=principal,
            request=request,
            idempotency_key=idempotency_key,
        )
    )


class RevokeMcpVersionRequest(BaseModel):
    reason: str


class McpToolResponse(BaseModel):
    id: UUID
    logical_key: str
    tool_name: str
    description: str
    side_effect: ToolSideEffect
    input_schema: dict
    schema_digest: str
    created_at: datetime


class McpVersionResponse(BaseModel):
    id: UUID
    semantic_version: str
    protocol_version: str
    configuration_digest: str
    status: McpServerVersionStatus
    created_at: datetime
    published_at: datetime | None
    revoked_at: datetime | None
    revoke_reason: str | None
    revision: int
    tools: list[McpToolResponse]


class McpServerResponse(BaseModel):
    id: UUID
    tenant_id: str
    owner_id: str
    name: str
    description: str
    transport: McpTransport
    endpoint_reference: str
    authentication_required: bool
    status: McpServerStatus
    created_at: datetime
    updated_at: datetime
    revision: int
    versions: list[McpVersionResponse]


class McpDiscoveredToolResponse(BaseModel):
    name: str
    schema_digest: str
    read_only_hint: bool | None
    idempotent_hint: bool | None


class McpDiscoverySnapshotResponse(BaseModel):
    id: UUID
    tenant_id: str
    server_id: UUID
    server_version_id: UUID
    configuration_digest: str
    protocol_version: str
    server_name: str
    status: McpDiscoveryStatus
    capability_digest: str | None
    discovered_tools: list[McpDiscoveredToolResponse]
    error: str | None
    fetched_at: datetime
    expires_at: datetime
    created_by: str


@registry_router.post(
    "/servers",
    response_model=McpServerResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(Permission.MCP_REGISTRY_MANAGE))],
)
def create_mcp_server(
    request: CreateMcpServerRequest,
    principal: PrincipalDependency,
    service: McpRegistryServiceDependency,
    idempotency_key: IdempotencyKey,
) -> McpServerResponse:
    server = service.register_server(
        owner_id=request.owner_id,
        name=request.name,
        description=request.description,
        transport=request.transport,
        endpoint_reference=request.endpoint_reference,
        actor=principal.principal_id,
        idempotency_key=idempotency_key,
        authentication_required=request.authentication_required,
    )
    return _server_response(McpServerView(server=server, versions=()))


@registry_router.get(
    "/servers",
    response_model=list[McpServerResponse],
    dependencies=[Depends(require_permission(Permission.MCP_REGISTRY_READ))],
)
def list_mcp_servers(
    service: McpRegistryServiceDependency,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[McpServerResponse]:
    return [_server_response(value) for value in service.list_servers(limit=limit, offset=offset)]


@registry_router.post(
    "/servers/{server_id}/versions",
    response_model=McpVersionResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(Permission.MCP_REGISTRY_MANAGE))],
)
def create_mcp_version(
    server_id: UUID,
    request: CreateMcpVersionRequest,
    principal: PrincipalDependency,
    service: McpRegistryServiceDependency,
    idempotency_key: IdempotencyKey,
) -> McpVersionResponse:
    value = service.add_version(
        server_id,
        semantic_version=request.semantic_version,
        protocol_version=request.protocol_version,
        configuration=request.configuration,
        actor=principal.principal_id,
        idempotency_key=idempotency_key,
    )
    return _version_response(value, ())


@registry_router.post(
    "/server-versions/{version_id}/tools",
    response_model=McpToolResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(Permission.MCP_REGISTRY_MANAGE))],
)
def create_mcp_tool(
    version_id: UUID,
    request: CreateMcpToolRequest,
    principal: PrincipalDependency,
    service: McpRegistryServiceDependency,
    idempotency_key: IdempotencyKey,
) -> McpToolResponse:
    return _tool_response(
        service.add_tool(
            version_id,
            logical_key=request.logical_key,
            tool_name=request.tool_name,
            description=request.description,
            side_effect=request.side_effect,
            input_schema=request.input_schema,
            actor=principal.principal_id,
            idempotency_key=idempotency_key,
        )
    )


@registry_router.post(
    "/server-versions/{version_id}/publish",
    response_model=McpVersionResponse,
    dependencies=[Depends(require_permission(Permission.MCP_REGISTRY_PUBLISH))],
)
def publish_mcp_version(
    version_id: UUID,
    principal: PrincipalDependency,
    service: McpRegistryServiceDependency,
    permit_id: ExecutionPermitId = None,
) -> McpVersionResponse:
    value = service.publish_version(version_id, principal=principal, permit_id=permit_id)
    view = next(
        (
            version_view
            for server in service.list_servers(limit=200, offset=0)
            for version_view in server.versions
            if version_view[0].id == value.id
        ),
        (value, ()),
    )
    return _version_response(*view)


@registry_router.post(
    "/server-versions/{version_id}/revoke",
    response_model=McpVersionResponse,
    dependencies=[Depends(require_permission(Permission.MCP_REGISTRY_PUBLISH))],
)
def revoke_mcp_version(
    version_id: UUID,
    request: RevokeMcpVersionRequest,
    principal: PrincipalDependency,
    service: McpRegistryServiceDependency,
) -> McpVersionResponse:
    value = service.revoke_version(version_id, reason=request.reason, actor=principal.principal_id)
    return _version_response(value, ())


@registry_router.post(
    "/server-versions/{version_id}/discovery-snapshots",
    response_model=McpDiscoverySnapshotResponse,
    dependencies=[Depends(require_permission(Permission.MCP_REGISTRY_MANAGE))],
)
def refresh_mcp_discovery(
    version_id: UUID,
    principal: PrincipalDependency,
    service: McpRegistryServiceDependency,
    idempotency_key: IdempotencyKey,
) -> McpDiscoverySnapshotResponse:
    return _discovery_response(
        service.refresh_discovery(
            version_id,
            principal=principal,
            idempotency_key=idempotency_key,
        )
    )


@registry_router.get(
    "/server-versions/{version_id}/discovery-snapshots",
    response_model=list[McpDiscoverySnapshotResponse],
    dependencies=[Depends(require_permission(Permission.MCP_REGISTRY_READ))],
)
def list_mcp_discovery_snapshots(
    version_id: UUID,
    service: McpRegistryServiceDependency,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[McpDiscoverySnapshotResponse]:
    return [
        _discovery_response(value)
        for value in service.list_discovery_snapshots(
            version_id, limit=limit, offset=offset
        )
    ]


@registry_router.post(
    "/servers/{server_id}/suspend",
    response_model=McpServerResponse,
    dependencies=[Depends(require_permission(Permission.MCP_REGISTRY_PUBLISH))],
)
def suspend_mcp_server(
    server_id: UUID,
    principal: PrincipalDependency,
    service: McpRegistryServiceDependency,
) -> McpServerResponse:
    server = service.suspend_server(server_id, actor=principal.principal_id)
    return _server_response(McpServerView(server=server, versions=()))


def _tool_response(value: McpToolCapability) -> McpToolResponse:
    return McpToolResponse(
        id=value.id,
        logical_key=value.logical_key,
        tool_name=value.tool_name,
        description=value.description,
        side_effect=value.side_effect,
        input_schema=value.input_schema,
        schema_digest=value.schema_digest,
        created_at=value.created_at,
    )


def _discovered_tool_response(value: McpDiscoveredTool) -> McpDiscoveredToolResponse:
    return McpDiscoveredToolResponse(
        name=value.name,
        schema_digest=value.schema_digest,
        read_only_hint=value.read_only_hint,
        idempotent_hint=value.idempotent_hint,
    )


def _discovery_response(value: McpDiscoverySnapshot) -> McpDiscoverySnapshotResponse:
    return McpDiscoverySnapshotResponse(
        id=value.id,
        tenant_id=value.tenant_id,
        server_id=value.server_id,
        server_version_id=value.server_version_id,
        configuration_digest=value.configuration_digest,
        protocol_version=value.protocol_version,
        server_name=value.server_name,
        status=value.status,
        capability_digest=value.capability_digest,
        discovered_tools=[_discovered_tool_response(tool) for tool in value.discovered_tools],
        error=value.error,
        fetched_at=value.fetched_at,
        expires_at=value.expires_at,
        created_by=value.created_by,
    )


def _version_response(
    value: McpServerVersion, tools: tuple[McpToolCapability, ...]
) -> McpVersionResponse:
    return McpVersionResponse(
        id=value.id,
        semantic_version=value.semantic_version,
        protocol_version=value.protocol_version,
        configuration_digest=value.configuration_digest,
        status=value.status,
        created_at=value.created_at,
        published_at=value.published_at,
        revoked_at=value.revoked_at,
        revoke_reason=value.revoke_reason,
        revision=value.revision,
        tools=[_tool_response(tool) for tool in tools],
    )


def _server_response(value: McpServerView) -> McpServerResponse:
    server = value.server
    return McpServerResponse(
        id=server.id,
        tenant_id=server.tenant_id,
        owner_id=server.owner_id,
        name=server.name,
        description=server.description,
        transport=server.transport,
        endpoint_reference=server.endpoint_reference,
        authentication_required=server.authentication_required,
        status=server.status,
        created_at=server.created_at,
        updated_at=server.updated_at,
        revision=server.revision,
        versions=[_version_response(version, tools) for version, tools in value.versions],
    )
