from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, status

from agentmesh.api.business_object_schemas import BusinessObjectSnapshotResponse
from agentmesh.api.company_pack_schemas import (
    ActivateMarketIntelligenceOperationsRequest,
    AppointMarketIntelligenceWorkforceRequest,
    CompanyOperationsPreviewResponse,
    CompanyTemplateInstallationResponse,
    CompanyTemplatePreviewResponse,
    CompanyWorkforceAppointmentsResponse,
    CompanyWorkforcePreviewResponse,
    InstallMarketIntelligenceTemplateRequest,
    InstallMusicStudioTemplateRequest,
    LaunchMarketResearchRequest,
    MarketResearchLaunchResponse,
    MarketResearchPreflightResponse,
    PackInstallationResponse,
    ResearchMaterializationResponse,
)
from agentmesh.api.company_schemas import AppointmentResponse
from agentmesh.api.feature_routes import require_feature
from agentmesh.api.schemas import TaskResponse
from agentmesh.api.security import (
    PrincipalDependency,
    require_permission,
    require_read_or_write_permission,
)
from agentmesh.application.company_pack_services import CompanyPackService
from agentmesh.application.market_research_services import MarketResearchService
from agentmesh.application.research_materialization_services import (
    ResearchMaterializationService,
)
from agentmesh.domain.identity import Permission
from agentmesh.features import Feature
from agentmesh.templates.market_intelligence_studio import TEMPLATE_SLUG
from agentmesh.templates.music_studio import TEMPLATE_SLUG as MUSIC_STUDIO_TEMPLATE_SLUG

router = APIRouter(
    prefix="/api/v1/company-templates",
    tags=["company-templates"],
    dependencies=[
        Depends(require_feature(Feature.COMPANY_PACKS)),
        Depends(
            require_read_or_write_permission(Permission.COMPANY_READ, Permission.COMPANY_MANAGE)
        ),
    ],
)


def get_service(request: Request) -> CompanyPackService:
    return request.app.state.container.company_pack_service


ServiceDependency = Annotated[CompanyPackService, Depends(get_service)]


def get_research_service(request: Request) -> MarketResearchService:
    return request.app.state.container.market_research_service


ResearchServiceDependency = Annotated[MarketResearchService, Depends(get_research_service)]


def get_research_materialization_service(request: Request) -> ResearchMaterializationService:
    return request.app.state.container.research_materialization_service


ResearchMaterializationServiceDependency = Annotated[
    ResearchMaterializationService, Depends(get_research_materialization_service)
]
IdempotencyHeader = Annotated[str, Header(alias="Idempotency-Key", max_length=255)]


@router.get("", response_model=list[CompanyTemplatePreviewResponse])
def list_templates(
    service: ServiceDependency,
) -> list[CompanyTemplatePreviewResponse]:
    return [
        CompanyTemplatePreviewResponse.from_domain(service.preview_music_studio_template()),
        CompanyTemplatePreviewResponse.from_domain(service.preview_market_intelligence_template())
    ]


@router.get(
    f"/{MUSIC_STUDIO_TEMPLATE_SLUG}/preview",
    response_model=CompanyTemplatePreviewResponse,
)
def preview_music_studio_template(
    service: ServiceDependency,
) -> CompanyTemplatePreviewResponse:
    return CompanyTemplatePreviewResponse.from_domain(service.preview_music_studio_template())


@router.post(
    f"/{MUSIC_STUDIO_TEMPLATE_SLUG}/install",
    response_model=CompanyTemplateInstallationResponse,
    status_code=status.HTTP_201_CREATED,
)
def install_music_studio_template(
    payload: InstallMusicStudioTemplateRequest,
    service: ServiceDependency,
    principal: PrincipalDependency,
) -> CompanyTemplateInstallationResponse:
    return CompanyTemplateInstallationResponse.from_domain(
        service.install_music_studio_template(
            **payload.model_dump(),
            owner_principal_id=principal.principal_id,
        )
    )


@router.get(
    f"/{TEMPLATE_SLUG}/preview",
    response_model=CompanyTemplatePreviewResponse,
)
def preview_template(
    service: ServiceDependency,
) -> CompanyTemplatePreviewResponse:
    return CompanyTemplatePreviewResponse.from_domain(
        service.preview_market_intelligence_template()
    )


@router.post(
    f"/{TEMPLATE_SLUG}/install",
    response_model=CompanyTemplateInstallationResponse,
    status_code=status.HTTP_201_CREATED,
)
def install_template(
    payload: InstallMarketIntelligenceTemplateRequest,
    service: ServiceDependency,
    principal: PrincipalDependency,
) -> CompanyTemplateInstallationResponse:
    return CompanyTemplateInstallationResponse.from_domain(
        service.install_market_intelligence_template(
            **payload.model_dump(),
            owner_principal_id=principal.principal_id,
        )
    )


@router.get(
    f"/{TEMPLATE_SLUG}/operations/preview",
    response_model=CompanyOperationsPreviewResponse,
)
def preview_operations(
    service: ServiceDependency,
) -> CompanyOperationsPreviewResponse:
    return CompanyOperationsPreviewResponse.from_domain(
        service.preview_market_intelligence_operations()
    )


@router.post(
    f"/{TEMPLATE_SLUG}/operations/activate",
    response_model=PackInstallationResponse,
    status_code=status.HTTP_201_CREATED,
)
def activate_operations(
    payload: ActivateMarketIntelligenceOperationsRequest,
    service: ServiceDependency,
    principal: PrincipalDependency,
) -> PackInstallationResponse:
    return PackInstallationResponse.model_validate(
        service.activate_market_intelligence_operations(
            **payload.model_dump(),
            installed_by=principal.principal_id,
        )
    )


@router.get(
    f"/{TEMPLATE_SLUG}/workforce/preview",
    response_model=CompanyWorkforcePreviewResponse,
)
def preview_workforce(
    service: ServiceDependency,
) -> CompanyWorkforcePreviewResponse:
    return CompanyWorkforcePreviewResponse.from_domain(
        service.preview_market_intelligence_workforce()
    )


@router.post(
    f"/{TEMPLATE_SLUG}/workforce/appoint",
    response_model=CompanyWorkforceAppointmentsResponse,
    status_code=status.HTTP_201_CREATED,
)
def appoint_workforce(
    payload: AppointMarketIntelligenceWorkforceRequest,
    service: ServiceDependency,
    principal: PrincipalDependency,
) -> CompanyWorkforceAppointmentsResponse:
    appointments = service.appoint_market_intelligence_workforce(
        assignments=[
            value.model_dump(mode="json") for value in payload.assignments
        ],
        appointed_by=principal.principal_id,
        reason=payload.reason,
    )
    return CompanyWorkforceAppointmentsResponse(
        appointments=[
            AppointmentResponse.from_domain(value) for value in appointments
        ]
    )


@router.get(
    f"/{TEMPLATE_SLUG}/research/preflight",
    response_model=MarketResearchPreflightResponse,
)
def preflight_research(
    service: ResearchServiceDependency,
) -> MarketResearchPreflightResponse:
    return MarketResearchPreflightResponse.from_domain(service.preflight())


@router.post(
    f"/{TEMPLATE_SLUG}/research/launch",
    response_model=MarketResearchLaunchResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        Depends(require_permission(Permission.TASK_CREATE)),
        Depends(require_permission(Permission.TASK_OPERATE)),
    ],
)
def launch_research(
    payload: LaunchMarketResearchRequest,
    service: ResearchServiceDependency,
    principal: PrincipalDependency,
    idempotency_key: IdempotencyHeader,
) -> MarketResearchLaunchResponse:
    result = service.launch(
        **payload.model_dump(),
        requested_by=principal.principal_id,
        idempotency_key=idempotency_key,
    )
    return MarketResearchLaunchResponse(
        task=TaskResponse.from_aggregate(result.task),
        research_question=BusinessObjectSnapshotResponse.from_snapshot(
            result.research_question
        ),
        preflight=MarketResearchPreflightResponse.from_domain(result.preflight),
    )


@router.get(
    f"/{TEMPLATE_SLUG}/research/tasks/{{task_id}}/materialization",
    response_model=ResearchMaterializationResponse,
)
def get_research_materialization(
    task_id: UUID,
    service: ResearchMaterializationServiceDependency,
) -> ResearchMaterializationResponse:
    return ResearchMaterializationResponse.from_domain(service.status(task_id))


@router.post(
    f"/{TEMPLATE_SLUG}/research/tasks/{{task_id}}/materialize",
    response_model=ResearchMaterializationResponse,
    dependencies=[Depends(require_permission(Permission.TASK_OPERATE))],
)
def materialize_research(
    task_id: UUID,
    service: ResearchMaterializationServiceDependency,
    principal: PrincipalDependency,
) -> ResearchMaterializationResponse:
    return ResearchMaterializationResponse.from_domain(
        service.materialize(task_id, actor=principal.principal_id)
    )
