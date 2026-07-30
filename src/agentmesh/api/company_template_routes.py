from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from agentmesh.api.company_pack_schemas import (
    ActivateMarketIntelligenceOperationsRequest,
    AppointMarketIntelligenceWorkforceRequest,
    CompanyOperationsPreviewResponse,
    CompanyTemplateInstallationResponse,
    CompanyTemplatePreviewResponse,
    CompanyWorkforceAppointmentsResponse,
    CompanyWorkforcePreviewResponse,
    InstallMarketIntelligenceTemplateRequest,
    PackInstallationResponse,
)
from agentmesh.api.company_schemas import AppointmentResponse
from agentmesh.api.feature_routes import require_feature
from agentmesh.api.security import (
    PrincipalDependency,
    require_read_or_write_permission,
)
from agentmesh.application.company_pack_services import CompanyPackService
from agentmesh.domain.identity import Permission
from agentmesh.features import Feature
from agentmesh.templates.market_intelligence_studio import TEMPLATE_SLUG

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


@router.get("", response_model=list[CompanyTemplatePreviewResponse])
def list_templates(
    service: ServiceDependency,
) -> list[CompanyTemplatePreviewResponse]:
    return [
        CompanyTemplatePreviewResponse.from_domain(service.preview_market_intelligence_template())
    ]


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
