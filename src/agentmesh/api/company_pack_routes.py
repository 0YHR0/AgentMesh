from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status

from agentmesh.api.company_pack_schemas import (
    CreatePackRequest,
    InstallPackRequest,
    PackInstallationResponse,
    PackPreviewResponse,
    PackResponse,
)
from agentmesh.api.feature_routes import require_feature
from agentmesh.api.security import PrincipalDependency, require_read_or_write_permission
from agentmesh.application.company_pack_services import CompanyPackService
from agentmesh.domain.identity import Permission
from agentmesh.features import Feature

router = APIRouter(
    prefix="/api/v1/company-packs",
    tags=["company-packs"],
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


@router.post("", response_model=PackResponse, status_code=status.HTTP_201_CREATED)
def create_pack(
    payload: CreatePackRequest, service: ServiceDependency
) -> PackResponse:
    return PackResponse.model_validate(service.create_pack(**payload.model_dump()))


@router.get("", response_model=list[PackResponse])
def list_packs(service: ServiceDependency) -> list[PackResponse]:
    return [PackResponse.model_validate(value) for value in service.list_packs()]


@router.post("/{pack_id}/publish", response_model=PackResponse)
def publish_pack(pack_id: UUID, service: ServiceDependency) -> PackResponse:
    return PackResponse.model_validate(service.publish_pack(pack_id))


@router.get(
    "/{pack_id}/companies/{company_id}/preview",
    response_model=PackPreviewResponse,
    dependencies=[
        Depends(
            require_read_or_write_permission(
                Permission.COMPANY_READ, Permission.COMPANY_MANAGE
            )
        )
    ],
)
def preview(
    pack_id: UUID, company_id: UUID, service: ServiceDependency
) -> PackPreviewResponse:
    return PackPreviewResponse.from_domain(service.preview(company_id, pack_id))


@router.post(
    "/{pack_id}/companies/{company_id}/install",
    response_model=PackInstallationResponse,
    dependencies=[
        Depends(
            require_read_or_write_permission(
                Permission.COMPANY_READ, Permission.COMPANY_MANAGE
            )
        )
    ],
)
def install(
    pack_id: UUID,
    company_id: UUID,
    payload: InstallPackRequest,
    service: ServiceDependency,
    principal: PrincipalDependency,
) -> PackInstallationResponse:
    return PackInstallationResponse.model_validate(
        service.install(
            company_id,
            pack_id,
            expected_digest=payload.expected_digest,
            installed_by=principal.principal_id,
        )
    )


@router.get(
    "/companies/{company_id}/installations",
    response_model=list[PackInstallationResponse],
)
def list_installations(
    company_id: UUID, service: ServiceDependency
) -> list[PackInstallationResponse]:
    return [
        PackInstallationResponse.model_validate(value)
        for value in service.list_installations(company_id)
    ]
