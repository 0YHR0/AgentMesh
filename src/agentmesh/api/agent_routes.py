from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status

from agentmesh.api.agent_schemas import (
    AffectedRunResponse,
    AgentCandidateResponse,
    AgentDefinitionListResponse,
    AgentDefinitionResponse,
    AgentDeploymentResponse,
    AgentInstanceHeartbeatRequest,
    AgentInstanceResponse,
    AgentVersionResponse,
    CandidateSearchRequest,
    CapabilityListResponse,
    CapabilityResponse,
    CreateAgentDefinitionRequest,
    CreateAgentDeploymentRequest,
    CreateAgentVersionRequest,
    CreateCapabilityRequest,
    PublishAgentVersionRequest,
    RevokeAgentVersionRequest,
    SetDefaultVersionRequest,
    UpdateAgentDeploymentStatusRequest,
)
from agentmesh.application.registry_services import AgentRegistryService

router = APIRouter(prefix="/api/v1", tags=["agent-registry"])


def get_registry_service(request: Request) -> AgentRegistryService:
    return request.app.state.container.registry_service


RegistryServiceDependency = Annotated[AgentRegistryService, Depends(get_registry_service)]
LimitQuery = Annotated[int, Query(ge=1, le=100)]
OffsetQuery = Annotated[int, Query(ge=0)]


@router.post(
    "/agents",
    response_model=AgentDefinitionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_definition(
    payload: CreateAgentDefinitionRequest,
    service: RegistryServiceDependency,
) -> AgentDefinitionResponse:
    return AgentDefinitionResponse.from_aggregate(service.create_definition(**payload.model_dump()))


@router.get("/agents", response_model=AgentDefinitionListResponse)
def list_definitions(
    service: RegistryServiceDependency,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
) -> AgentDefinitionListResponse:
    values = service.list_definitions(limit=limit, offset=offset)
    return AgentDefinitionListResponse(
        items=[AgentDefinitionResponse.from_aggregate(value) for value in values],
        limit=limit,
        offset=offset,
    )


@router.get("/agents/{definition_id}", response_model=AgentDefinitionResponse)
def get_definition(
    definition_id: UUID,
    service: RegistryServiceDependency,
) -> AgentDefinitionResponse:
    return AgentDefinitionResponse.from_aggregate(service.get_definition(definition_id))


@router.post("/agents/{definition_id}/archive", response_model=AgentDefinitionResponse)
def archive_definition(
    definition_id: UUID,
    service: RegistryServiceDependency,
) -> AgentDefinitionResponse:
    return AgentDefinitionResponse.from_aggregate(service.archive_definition(definition_id))


@router.post(
    "/agents/{definition_id}/versions",
    response_model=AgentVersionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_version(
    definition_id: UUID,
    payload: CreateAgentVersionRequest,
    service: RegistryServiceDependency,
) -> AgentVersionResponse:
    return AgentVersionResponse.from_domain(
        service.create_version(definition_id, **payload.model_dump())
    )


@router.post(
    "/agent-versions/{agent_version_id}/submit-review",
    response_model=AgentVersionResponse,
)
def submit_version(
    agent_version_id: UUID,
    service: RegistryServiceDependency,
) -> AgentVersionResponse:
    return AgentVersionResponse.from_domain(service.submit_version(agent_version_id))


@router.post(
    "/agent-versions/{agent_version_id}/reject",
    response_model=AgentVersionResponse,
)
def reject_version(
    agent_version_id: UUID,
    service: RegistryServiceDependency,
) -> AgentVersionResponse:
    return AgentVersionResponse.from_domain(service.reject_version(agent_version_id))


@router.post(
    "/agent-versions/{agent_version_id}/publish",
    response_model=AgentVersionResponse,
)
def publish_version(
    agent_version_id: UUID,
    payload: PublishAgentVersionRequest,
    service: RegistryServiceDependency,
) -> AgentVersionResponse:
    return AgentVersionResponse.from_domain(
        service.publish_version(agent_version_id, **payload.model_dump())
    )


@router.post(
    "/agent-versions/{agent_version_id}/deprecate",
    response_model=AgentVersionResponse,
)
def deprecate_version(
    agent_version_id: UUID,
    service: RegistryServiceDependency,
) -> AgentVersionResponse:
    return AgentVersionResponse.from_domain(service.deprecate_version(agent_version_id))


@router.post(
    "/agent-versions/{agent_version_id}/retire",
    response_model=AgentVersionResponse,
)
def retire_version(
    agent_version_id: UUID,
    service: RegistryServiceDependency,
) -> AgentVersionResponse:
    return AgentVersionResponse.from_domain(service.retire_version(agent_version_id))


@router.post(
    "/agent-versions/{agent_version_id}/revoke",
    response_model=AgentVersionResponse,
)
def revoke_version(
    agent_version_id: UUID,
    payload: RevokeAgentVersionRequest,
    service: RegistryServiceDependency,
) -> AgentVersionResponse:
    return AgentVersionResponse.from_domain(
        service.revoke_version(agent_version_id, reason=payload.reason)
    )


@router.get(
    "/agent-versions/{agent_version_id}/affected-runs",
    response_model=list[AffectedRunResponse],
)
def list_affected_runs(
    agent_version_id: UUID,
    service: RegistryServiceDependency,
) -> list[AffectedRunResponse]:
    return [
        AffectedRunResponse.from_domain(value)
        for value in service.list_affected_active_runs(agent_version_id)
    ]


@router.put(
    "/agents/{definition_id}/default-version",
    response_model=AgentDefinitionResponse,
)
def set_default_version(
    definition_id: UUID,
    payload: SetDefaultVersionRequest,
    service: RegistryServiceDependency,
) -> AgentDefinitionResponse:
    return AgentDefinitionResponse.from_aggregate(
        service.set_default_version(definition_id, payload.agent_version_id)
    )


@router.post(
    "/capabilities",
    response_model=CapabilityResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_capability(
    payload: CreateCapabilityRequest,
    service: RegistryServiceDependency,
) -> CapabilityResponse:
    return CapabilityResponse.from_domain(service.create_capability(**payload.model_dump()))


@router.get("/capabilities", response_model=CapabilityListResponse)
def list_capabilities(
    service: RegistryServiceDependency,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
) -> CapabilityListResponse:
    values = service.list_capabilities(limit=limit, offset=offset)
    return CapabilityListResponse(
        items=[CapabilityResponse.from_domain(value) for value in values],
        limit=limit,
        offset=offset,
    )


@router.post("/agent-candidates:search", response_model=list[AgentCandidateResponse])
def search_candidates(
    payload: CandidateSearchRequest,
    service: RegistryServiceDependency,
) -> list[AgentCandidateResponse]:
    return [
        AgentCandidateResponse.from_domain(value)
        for value in service.find_candidates(**payload.model_dump())
    ]


@router.post(
    "/agent-versions/{agent_version_id}/deployments",
    response_model=AgentDeploymentResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_deployment(
    agent_version_id: UUID,
    payload: CreateAgentDeploymentRequest,
    service: RegistryServiceDependency,
) -> AgentDeploymentResponse:
    return AgentDeploymentResponse.from_domain(
        service.create_deployment(agent_version_id, **payload.model_dump())
    )


@router.get(
    "/agent-versions/{agent_version_id}/deployments",
    response_model=list[AgentDeploymentResponse],
)
def list_deployments(
    agent_version_id: UUID,
    service: RegistryServiceDependency,
) -> list[AgentDeploymentResponse]:
    return [
        AgentDeploymentResponse.from_domain(value)
        for value in service.list_deployments(agent_version_id)
    ]


@router.patch(
    "/agent-deployments/{deployment_id}/status",
    response_model=AgentDeploymentResponse,
)
def update_deployment_status(
    deployment_id: UUID,
    payload: UpdateAgentDeploymentStatusRequest,
    service: RegistryServiceDependency,
) -> AgentDeploymentResponse:
    return AgentDeploymentResponse.from_domain(
        service.update_deployment_status(deployment_id, **payload.model_dump())
    )


@router.put(
    "/internal/agent-deployments/{deployment_id}/instances/{external_instance_id}/heartbeat",
    response_model=AgentInstanceResponse,
)
def heartbeat_instance(
    deployment_id: UUID,
    external_instance_id: str,
    payload: AgentInstanceHeartbeatRequest,
    service: RegistryServiceDependency,
) -> AgentInstanceResponse:
    return AgentInstanceResponse.from_domain(
        service.heartbeat_instance(
            deployment_id,
            external_instance_id=external_instance_id,
            **payload.model_dump(),
        )
    )


@router.get(
    "/agent-deployments/{deployment_id}/instances",
    response_model=list[AgentInstanceResponse],
)
def list_instances(
    deployment_id: UUID,
    service: RegistryServiceDependency,
) -> list[AgentInstanceResponse]:
    return [
        AgentInstanceResponse.from_domain(value) for value in service.list_instances(deployment_id)
    ]
