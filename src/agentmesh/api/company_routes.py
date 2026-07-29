from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status

from agentmesh.api.company_schemas import (
    AppointmentResponse,
    CompanyResponse,
    CompanySnapshotResponse,
    CreateAppointmentRequest,
    CreateCompanyRequest,
    CreateOrganizationRelationshipRequest,
    CreateOrganizationUnitRequest,
    CreatePositionRequest,
    OrganizationRelationshipResponse,
    OrganizationUnitResponse,
    PositionResponse,
)
from agentmesh.api.feature_routes import require_feature
from agentmesh.api.security import PrincipalDependency, require_read_or_write_permission
from agentmesh.application.company_services import CompanyModelService
from agentmesh.domain.identity import Permission
from agentmesh.features import Feature

router = APIRouter(
    prefix="/api/v1",
    tags=["company-model"],
    dependencies=[
        Depends(
            require_read_or_write_permission(
                Permission.COMPANY_READ,
                Permission.COMPANY_MANAGE,
            )
        ),
        Depends(require_feature(Feature.COMPANY_MODEL)),
    ],
)


def get_company_service(request: Request) -> CompanyModelService:
    return request.app.state.container.company_service


CompanyServiceDependency = Annotated[CompanyModelService, Depends(get_company_service)]


@router.post("/companies", response_model=CompanyResponse, status_code=status.HTTP_201_CREATED)
def create_company(
    payload: CreateCompanyRequest,
    service: CompanyServiceDependency,
    principal: PrincipalDependency,
) -> CompanyResponse:
    return CompanyResponse.from_domain(
        service.create_company(
            **payload.model_dump(),
            owner_principal_id=principal.principal_id,
        )
    )


@router.get("/companies", response_model=list[CompanyResponse])
def list_companies(service: CompanyServiceDependency) -> list[CompanyResponse]:
    return [CompanyResponse.from_domain(value) for value in service.list_companies()]


@router.get("/companies/active", response_model=CompanySnapshotResponse)
def get_active_company(service: CompanyServiceDependency) -> CompanySnapshotResponse:
    return CompanySnapshotResponse.from_snapshot(service.get_active_company())


@router.get("/companies/{company_id}", response_model=CompanySnapshotResponse)
def get_company(
    company_id: UUID, service: CompanyServiceDependency
) -> CompanySnapshotResponse:
    return CompanySnapshotResponse.from_snapshot(service.get_company(company_id))


@router.post("/companies/{company_id}/archive", response_model=CompanyResponse)
def archive_company(
    company_id: UUID, service: CompanyServiceDependency
) -> CompanyResponse:
    return CompanyResponse.from_domain(service.archive_company(company_id))


@router.post(
    "/companies/{company_id}/units",
    response_model=OrganizationUnitResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_unit(
    company_id: UUID,
    payload: CreateOrganizationUnitRequest,
    service: CompanyServiceDependency,
) -> OrganizationUnitResponse:
    return OrganizationUnitResponse.from_domain(
        service.create_unit(company_id, **payload.model_dump())
    )


@router.post(
    "/companies/{company_id}/positions",
    response_model=PositionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_position(
    company_id: UUID,
    payload: CreatePositionRequest,
    service: CompanyServiceDependency,
) -> PositionResponse:
    return PositionResponse.from_domain(
        service.create_position(company_id, **payload.model_dump())
    )


@router.post(
    "/companies/{company_id}/appointments",
    response_model=AppointmentResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_appointment(
    company_id: UUID,
    payload: CreateAppointmentRequest,
    service: CompanyServiceDependency,
    principal: PrincipalDependency,
) -> AppointmentResponse:
    return AppointmentResponse.from_domain(
        service.appoint(
            company_id,
            **payload.model_dump(),
            appointed_by=principal.principal_id,
        )
    )


@router.post(
    "/companies/{company_id}/appointments/{appointment_id}/end",
    response_model=AppointmentResponse,
)
def end_appointment(
    company_id: UUID,
    appointment_id: UUID,
    service: CompanyServiceDependency,
) -> AppointmentResponse:
    return AppointmentResponse.from_domain(
        service.end_appointment(company_id, appointment_id)
    )


@router.post(
    "/companies/{company_id}/relationships",
    response_model=OrganizationRelationshipResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_relationship(
    company_id: UUID,
    payload: CreateOrganizationRelationshipRequest,
    service: CompanyServiceDependency,
) -> OrganizationRelationshipResponse:
    return OrganizationRelationshipResponse.from_domain(
        service.create_relationship(company_id, **payload.model_dump())
    )
