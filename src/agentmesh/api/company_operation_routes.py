from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status

from agentmesh.api.company_operation_schemas import (
    ActivateStaffedOperationsRequest,
    CreateOperationRequest,
    DispatchOperationsRequest,
    OperationLaunchResponse,
    OperationResponse,
    OperationSnapshotResponse,
    OperationTransitionRequest,
    TriggerOperationRequest,
)
from agentmesh.api.feature_routes import require_feature
from agentmesh.api.security import require_read_or_write_permission
from agentmesh.application.company_operation_services import CompanyOperationService
from agentmesh.domain.identity import Permission
from agentmesh.features import Feature

router = APIRouter(
    prefix="/api/v1/companies/{company_id}/operations",
    tags=["company-operations"],
    dependencies=[
        Depends(
            require_read_or_write_permission(
                Permission.COMPANY_READ, Permission.COMPANY_MANAGE
            )
        ),
        Depends(require_feature(Feature.COMPANY_OPERATIONS)),
    ],
)


def get_service(request: Request) -> CompanyOperationService:
    return request.app.state.container.company_operation_service


ServiceDependency = Annotated[CompanyOperationService, Depends(get_service)]


@router.post("", response_model=OperationResponse, status_code=status.HTTP_201_CREATED)
def create_operation(
    company_id: UUID,
    payload: CreateOperationRequest,
    service: ServiceDependency,
) -> OperationResponse:
    return OperationResponse.model_validate(
        service.create_operation(company_id, **payload.model_dump())
    )


@router.get("", response_model=list[OperationResponse])
def list_operations(
    company_id: UUID, service: ServiceDependency
) -> list[OperationResponse]:
    return [
        OperationResponse.model_validate(value)
        for value in service.list_operations(company_id)
    ]


@router.post("/_dispatch/due", response_model=list[OperationLaunchResponse])
def dispatch_due(
    company_id: UUID,
    payload: DispatchOperationsRequest,
    service: ServiceDependency,
) -> list[OperationLaunchResponse]:
    # Scope validation happens through each due Operation's tenant-owned Company.
    service.list_operations(company_id)
    return [
        OperationLaunchResponse.from_launch(value)
        for value in service.dispatch_due(**payload.model_dump())
    ]


@router.post(
    "/_activate/staffed",
    response_model=list[OperationResponse],
)
def activate_staffed_operations(
    company_id: UUID,
    payload: ActivateStaffedOperationsRequest,
    service: ServiceDependency,
) -> list[OperationResponse]:
    return [
        OperationResponse.model_validate(value)
        for value in service.activate_staffed_operations(
            company_id, **payload.model_dump()
        )
    ]


@router.get("/{operation_id}", response_model=OperationSnapshotResponse)
def get_operation(
    company_id: UUID, operation_id: UUID, service: ServiceDependency
) -> OperationSnapshotResponse:
    return OperationSnapshotResponse.from_snapshot(
        service.get_operation(company_id, operation_id)
    )


@router.post("/{operation_id}/transition", response_model=OperationResponse)
def transition_operation(
    company_id: UUID,
    operation_id: UUID,
    payload: OperationTransitionRequest,
    service: ServiceDependency,
) -> OperationResponse:
    return OperationResponse.model_validate(
        service.transition_operation(company_id, operation_id, payload.action)
    )


@router.post("/{operation_id}/trigger", response_model=OperationLaunchResponse)
def trigger_operation(
    company_id: UUID,
    operation_id: UUID,
    payload: TriggerOperationRequest,
    service: ServiceDependency,
) -> OperationLaunchResponse:
    return OperationLaunchResponse.from_launch(
        service.trigger_manual(
            company_id, operation_id, **payload.model_dump()
        )
    )
