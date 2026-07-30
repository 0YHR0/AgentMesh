from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from agentmesh.api.company_pack_schemas import (
    CompanyTemplateInstallationResponse,
    CompanyTemplatePreviewResponse,
    InstallMarketIntelligenceTemplateRequest,
)
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
            require_read_or_write_permission(
                Permission.COMPANY_READ, Permission.COMPANY_MANAGE
            )
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
        CompanyTemplatePreviewResponse.from_domain(
            service.preview_market_intelligence_template()
        )
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
