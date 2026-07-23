from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, status

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
from agentmesh.api.feature_routes import require_feature
from agentmesh.api.policy_routes import PolicyServiceDependency
from agentmesh.api.security import (
    PrincipalDependency,
    require_permission,
    require_read_or_write_permission,
)
from agentmesh.application.registry_services import AgentRegistryService
from agentmesh.domain.identity import Permission
from agentmesh.domain.policy import GovernedActionType
from agentmesh.features import Feature

router = APIRouter(prefix="/api/v1")
registry_router = APIRouter(
    tags=["agent-registry"],
    dependencies=[
        Depends(require_read_or_write_permission(Permission.AGENT_READ, Permission.AGENT_MANAGE)),
        Depends(require_feature(Feature.AGENT_REGISTRY_MANAGEMENT)),
    ],
)
deployment_router = APIRouter(
    tags=["agent-deployments"],
    dependencies=[
        Depends(require_read_or_write_permission(Permission.AGENT_READ, Permission.AGENT_PUBLISH)),
        Depends(require_feature(Feature.AGENT_DEPLOYMENTS)),
    ],
)
lookup_router = APIRouter(
    tags=["agent-registry"],
    dependencies=[
        Depends(require_permission(Permission.AGENT_READ)),
        Depends(require_feature(Feature.AGENT_REGISTRY_MANAGEMENT)),
    ],
)


def get_registry_service(request: Request) -> AgentRegistryService:
    return request.app.state.container.registry_service


RegistryServiceDependency = Annotated[AgentRegistryService, Depends(get_registry_service)]
LimitQuery = Annotated[int, Query(ge=1, le=100)]
OffsetQuery = Annotated[int, Query(ge=0)]
PermitHeader = Annotated[UUID | None, Header(alias="Execution-Permit-Id")]


@registry_router.post(
    "/agents",
    response_model=AgentDefinitionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_definition(
    payload: CreateAgentDefinitionRequest,
    service: RegistryServiceDependency,
) -> AgentDefinitionResponse:
    return AgentDefinitionResponse.from_aggregate(service.create_definition(**payload.model_dump()))


@registry_router.get("/agents", response_model=AgentDefinitionListResponse)
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


@registry_router.get("/agents/{definition_id}", response_model=AgentDefinitionResponse)
def get_definition(
    definition_id: UUID,
    service: RegistryServiceDependency,
) -> AgentDefinitionResponse:
    return AgentDefinitionResponse.from_aggregate(service.get_definition(definition_id))


@registry_router.post(
    "/agents/{definition_id}/archive",
    response_model=AgentDefinitionResponse,
    dependencies=[Depends(require_permission(Permission.AGENT_PUBLISH))],
)
def archive_definition(
    definition_id: UUID,
    service: RegistryServiceDependency,
) -> AgentDefinitionResponse:
    return AgentDefinitionResponse.from_aggregate(service.archive_definition(definition_id))


@registry_router.post(
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


@registry_router.post(
    "/agent-versions/{agent_version_id}/submit-review",
    response_model=AgentVersionResponse,
)
def submit_version(
    agent_version_id: UUID,
    service: RegistryServiceDependency,
) -> AgentVersionResponse:
    return AgentVersionResponse.from_domain(service.submit_version(agent_version_id))


@registry_router.post(
    "/agent-versions/{agent_version_id}/reject",
    response_model=AgentVersionResponse,
    dependencies=[Depends(require_permission(Permission.AGENT_PUBLISH))],
)
def reject_version(
    agent_version_id: UUID,
    service: RegistryServiceDependency,
) -> AgentVersionResponse:
    return AgentVersionResponse.from_domain(service.reject_version(agent_version_id))


@registry_router.post(
    "/agent-versions/{agent_version_id}/publish",
    response_model=AgentVersionResponse,
    dependencies=[Depends(require_permission(Permission.AGENT_PUBLISH))],
)
def publish_version(
    agent_version_id: UUID,
    payload: PublishAgentVersionRequest,
    service: RegistryServiceDependency,
    policy_service: PolicyServiceDependency,
    principal: PrincipalDependency,
    permit_id: PermitHeader = None,
) -> AgentVersionResponse:
    arguments = {
        "verified_capabilities": payload.verified_capabilities,
        "make_default": payload.make_default,
    }
    policy_service.consume_permit(
        permit_id,
        principal=principal,
        action_type=GovernedActionType.AGENT_VERSION_PUBLISH,
        resource_type="agent_version",
        resource_id=agent_version_id,
        arguments=arguments,
    )
    return AgentVersionResponse.from_domain(
        service.publish_version(agent_version_id, **payload.model_dump())
    )


@registry_router.post(
    "/agent-versions/{agent_version_id}/deprecate",
    response_model=AgentVersionResponse,
    dependencies=[Depends(require_permission(Permission.AGENT_PUBLISH))],
)
def deprecate_version(
    agent_version_id: UUID,
    service: RegistryServiceDependency,
) -> AgentVersionResponse:
    return AgentVersionResponse.from_domain(service.deprecate_version(agent_version_id))


@registry_router.post(
    "/agent-versions/{agent_version_id}/retire",
    response_model=AgentVersionResponse,
    dependencies=[Depends(require_permission(Permission.AGENT_PUBLISH))],
)
def retire_version(
    agent_version_id: UUID,
    service: RegistryServiceDependency,
) -> AgentVersionResponse:
    return AgentVersionResponse.from_domain(service.retire_version(agent_version_id))


@registry_router.post(
    "/agent-versions/{agent_version_id}/revoke",
    response_model=AgentVersionResponse,
    dependencies=[Depends(require_permission(Permission.AGENT_PUBLISH))],
)
def revoke_version(
    agent_version_id: UUID,
    payload: RevokeAgentVersionRequest,
    service: RegistryServiceDependency,
) -> AgentVersionResponse:
    return AgentVersionResponse.from_domain(
        service.revoke_version(agent_version_id, reason=payload.reason)
    )


@registry_router.get(
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


@registry_router.put(
    "/agents/{definition_id}/default-version",
    response_model=AgentDefinitionResponse,
    dependencies=[Depends(require_permission(Permission.AGENT_PUBLISH))],
)
def set_default_version(
    definition_id: UUID,
    payload: SetDefaultVersionRequest,
    service: RegistryServiceDependency,
) -> AgentDefinitionResponse:
    return AgentDefinitionResponse.from_aggregate(
        service.set_default_version(definition_id, payload.agent_version_id)
    )


@registry_router.post(
    "/capabilities",
    response_model=CapabilityResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_capability(
    payload: CreateCapabilityRequest,
    service: RegistryServiceDependency,
) -> CapabilityResponse:
    return CapabilityResponse.from_domain(service.create_capability(**payload.model_dump()))


@registry_router.get("/capabilities", response_model=CapabilityListResponse)
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


@lookup_router.post("/agent-candidates:search", response_model=list[AgentCandidateResponse])
def search_candidates(
    payload: CandidateSearchRequest,
    service: RegistryServiceDependency,
) -> list[AgentCandidateResponse]:
    return [
        AgentCandidateResponse.from_domain(value)
        for value in service.find_candidates(**payload.model_dump())
    ]


@deployment_router.post(
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


@deployment_router.get(
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


@deployment_router.patch(
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


@deployment_router.put(
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


@deployment_router.get(
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


@deployment_router.post(
    "/agent-deployments/{deployment_id}/instances/reconcile",
    response_model=list[AgentInstanceResponse],
)
def reconcile_instances(
    deployment_id: UUID,
    service: RegistryServiceDependency,
    stale_after_seconds: Annotated[int, Query(ge=5, le=86_400)] = 60,
) -> list[AgentInstanceResponse]:
    return [
        AgentInstanceResponse.from_domain(value)
        for value in service.reconcile_instances(
            deployment_id, stale_after_seconds=stale_after_seconds
        )
    ]


router.include_router(registry_router)
router.include_router(lookup_router)
router.include_router(deployment_router)
