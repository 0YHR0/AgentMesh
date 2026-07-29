from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status

from agentmesh.api.company_goal_schemas import (
    CreateCycleRequest,
    CreateInitiativeRequest,
    CreateKeyResultRequest,
    CreateObjectiveRequest,
    CycleResponse,
    CycleSnapshotResponse,
    InitiativeResponse,
    InitiativeTaskLaunchResponse,
    KeyResultResponse,
    LaunchInitiativeTaskRequest,
    ObjectiveResponse,
    RecordKeyResultRequest,
    TransitionRequest,
)
from agentmesh.api.feature_routes import require_feature
from agentmesh.api.security import (
    PrincipalDependency,
    require_read_or_write_permission,
)
from agentmesh.application.company_goal_services import CompanyGoalService
from agentmesh.domain.identity import Permission
from agentmesh.features import Feature

router = APIRouter(
    prefix="/api/v1/companies/{company_id}",
    tags=["company-goals"],
    dependencies=[
        Depends(
            require_read_or_write_permission(
                Permission.COMPANY_READ, Permission.COMPANY_MANAGE
            )
        ),
        Depends(require_feature(Feature.COMPANY_GOALS)),
    ],
)


def get_service(request: Request) -> CompanyGoalService:
    return request.app.state.container.company_goal_service


ServiceDependency = Annotated[CompanyGoalService, Depends(get_service)]


@router.post("/cycles", response_model=CycleResponse, status_code=status.HTTP_201_CREATED)
def create_cycle(
    company_id: UUID, payload: CreateCycleRequest, service: ServiceDependency
) -> CycleResponse:
    return CycleResponse.model_validate(
        service.create_cycle(company_id, **payload.model_dump())
    )


@router.get("/cycles", response_model=list[CycleResponse])
def list_cycles(company_id: UUID, service: ServiceDependency) -> list[CycleResponse]:
    return [
        CycleResponse.model_validate(value) for value in service.list_cycles(company_id)
    ]


@router.get("/cycles/{cycle_id}", response_model=CycleSnapshotResponse)
def get_cycle(
    company_id: UUID, cycle_id: UUID, service: ServiceDependency
) -> CycleSnapshotResponse:
    return CycleSnapshotResponse.from_snapshot(service.get_cycle(company_id, cycle_id))


@router.post("/cycles/{cycle_id}/transition", response_model=CycleResponse)
def transition_cycle(
    company_id: UUID,
    cycle_id: UUID,
    payload: TransitionRequest,
    service: ServiceDependency,
    principal: PrincipalDependency,
) -> CycleResponse:
    return CycleResponse.model_validate(
        service.transition_cycle(
            company_id, cycle_id, payload.action, principal.principal_id
        )
    )


@router.post(
    "/cycles/{cycle_id}/objectives",
    response_model=ObjectiveResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_objective(
    company_id: UUID,
    cycle_id: UUID,
    payload: CreateObjectiveRequest,
    service: ServiceDependency,
) -> ObjectiveResponse:
    return ObjectiveResponse.model_validate(
        service.create_objective(company_id, cycle_id, **payload.model_dump())
    )


@router.post("/objectives/{objective_id}/transition", response_model=ObjectiveResponse)
def transition_objective(
    company_id: UUID,
    objective_id: UUID,
    payload: TransitionRequest,
    service: ServiceDependency,
) -> ObjectiveResponse:
    return ObjectiveResponse.model_validate(
        service.transition_objective(company_id, objective_id, payload.action)
    )


@router.post(
    "/objectives/{objective_id}/key-results",
    response_model=KeyResultResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_key_result(
    company_id: UUID,
    objective_id: UUID,
    payload: CreateKeyResultRequest,
    service: ServiceDependency,
) -> KeyResultResponse:
    return KeyResultResponse.model_validate(
        service.create_key_result(company_id, objective_id, **payload.model_dump())
    )


@router.post("/key-results/{key_result_id}/measurements", response_model=KeyResultResponse)
def record_key_result(
    company_id: UUID,
    key_result_id: UUID,
    payload: RecordKeyResultRequest,
    service: ServiceDependency,
) -> KeyResultResponse:
    return KeyResultResponse.model_validate(
        service.record_key_result(company_id, key_result_id, **payload.model_dump())
    )


@router.post(
    "/objectives/{objective_id}/initiatives",
    response_model=InitiativeResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_initiative(
    company_id: UUID,
    objective_id: UUID,
    payload: CreateInitiativeRequest,
    service: ServiceDependency,
) -> InitiativeResponse:
    return InitiativeResponse.model_validate(
        service.create_initiative(company_id, objective_id, **payload.model_dump())
    )


@router.post("/initiatives/{initiative_id}/transition", response_model=InitiativeResponse)
def transition_initiative(
    company_id: UUID,
    initiative_id: UUID,
    payload: TransitionRequest,
    service: ServiceDependency,
) -> InitiativeResponse:
    return InitiativeResponse.model_validate(
        service.transition_initiative(company_id, initiative_id, payload.action)
    )


@router.post(
    "/initiatives/{initiative_id}/tasks",
    response_model=InitiativeTaskLaunchResponse,
    status_code=status.HTTP_201_CREATED,
)
def launch_task(
    company_id: UUID,
    initiative_id: UUID,
    payload: LaunchInitiativeTaskRequest,
    service: ServiceDependency,
    principal: PrincipalDependency,
) -> InitiativeTaskLaunchResponse:
    return InitiativeTaskLaunchResponse.from_launch(
        service.launch_task(
            company_id,
            initiative_id,
            **payload.model_dump(),
            created_by=principal.principal_id,
        )
    )
