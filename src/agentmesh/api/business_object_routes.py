from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status

from agentmesh.api.business_object_schemas import (
    ApplyBusinessObjectActionRequest,
    BusinessObjectResponse,
    BusinessObjectSnapshotResponse,
    BusinessObjectTypeResponse,
    BusinessObjectTypeTransitionRequest,
    CreateBusinessObjectRequest,
    CreateBusinessObjectTypeRequest,
)
from agentmesh.api.feature_routes import require_feature
from agentmesh.api.security import PrincipalDependency, require_read_or_write_permission
from agentmesh.application.business_object_services import BusinessObjectService
from agentmesh.domain.identity import Permission
from agentmesh.features import Feature

router = APIRouter(
    prefix="/api/v1/companies/{company_id}",
    tags=["business-objects"],
    dependencies=[
        Depends(
            require_read_or_write_permission(
                Permission.COMPANY_READ, Permission.COMPANY_MANAGE
            )
        ),
        Depends(require_feature(Feature.BUSINESS_OBJECTS)),
    ],
)


def get_service(request: Request) -> BusinessObjectService:
    return request.app.state.container.business_object_service


ServiceDependency = Annotated[BusinessObjectService, Depends(get_service)]


@router.post(
    "/business-object-types",
    response_model=BusinessObjectTypeResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_type(
    company_id: UUID,
    payload: CreateBusinessObjectTypeRequest,
    service: ServiceDependency,
) -> BusinessObjectTypeResponse:
    return BusinessObjectTypeResponse.model_validate(
        service.create_type(company_id, **payload.model_dump())
    )


@router.get(
    "/business-object-types", response_model=list[BusinessObjectTypeResponse]
)
def list_types(
    company_id: UUID, service: ServiceDependency
) -> list[BusinessObjectTypeResponse]:
    return [
        BusinessObjectTypeResponse.model_validate(value)
        for value in service.list_types(company_id)
    ]


@router.post(
    "/business-object-types/{type_id}/transition",
    response_model=BusinessObjectTypeResponse,
)
def transition_type(
    company_id: UUID,
    type_id: UUID,
    payload: BusinessObjectTypeTransitionRequest,
    service: ServiceDependency,
) -> BusinessObjectTypeResponse:
    return BusinessObjectTypeResponse.model_validate(
        service.transition_type(company_id, type_id, payload.action)
    )


@router.post(
    "/business-objects",
    response_model=BusinessObjectSnapshotResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_object(
    company_id: UUID,
    payload: CreateBusinessObjectRequest,
    service: ServiceDependency,
    principal: PrincipalDependency,
) -> BusinessObjectSnapshotResponse:
    return BusinessObjectSnapshotResponse.from_snapshot(
        service.create_object(
            company_id,
            **payload.model_dump(),
            actor=principal.principal_id,
        )
    )


@router.get("/business-objects", response_model=list[BusinessObjectResponse])
def list_objects(
    company_id: UUID,
    service: ServiceDependency,
    type_id: UUID | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[BusinessObjectResponse]:
    return [
        BusinessObjectResponse.model_validate(value)
        for value in service.list_objects(
            company_id, type_id=type_id, limit=limit, offset=offset
        )
    ]


@router.get(
    "/business-objects/{object_id}", response_model=BusinessObjectSnapshotResponse
)
def get_object(
    company_id: UUID, object_id: UUID, service: ServiceDependency
) -> BusinessObjectSnapshotResponse:
    return BusinessObjectSnapshotResponse.from_snapshot(
        service.get_object(company_id, object_id)
    )


@router.post(
    "/business-objects/{object_id}/actions",
    response_model=BusinessObjectSnapshotResponse,
)
def apply_action(
    company_id: UUID,
    object_id: UUID,
    payload: ApplyBusinessObjectActionRequest,
    service: ServiceDependency,
    principal: PrincipalDependency,
) -> BusinessObjectSnapshotResponse:
    return BusinessObjectSnapshotResponse.from_snapshot(
        service.apply_action(
            company_id,
            object_id,
            **payload.model_dump(),
            actor=principal.principal_id,
        )
    )
