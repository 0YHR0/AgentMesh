from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from agentmesh.api.feature_routes import require_feature
from agentmesh.api.security import require_permission
from agentmesh.application.office_services import OfficeLayoutService
from agentmesh.domain.identity import Permission
from agentmesh.domain.office import (
    DEFAULT_OFFICE_GRID,
    DEFAULT_OFFICE_OBSTACLES,
    DEFAULT_OFFICE_ROOMS,
    InvalidOfficePlacement,
    OfficeCellOccupied,
    OfficePlacement,
)
from agentmesh.features import Feature

router = APIRouter(
    prefix="/api/v1/office-layout",
    tags=["office-layout"],
    dependencies=[Depends(require_feature(Feature.OFFICE_3D))],
)


def get_service(request: Request) -> OfficeLayoutService:
    return request.app.state.container.office_layout_service


ServiceDependency = Annotated[OfficeLayoutService, Depends(get_service)]


class OfficeGridResponse(BaseModel):
    cell_size: int
    origin_x: int
    origin_z: int
    columns: int
    rows: int


class OfficeRoomResponse(BaseModel):
    key: str
    grid_x: int
    grid_z: int
    width: int
    depth: int


class OfficePlacementResponse(BaseModel):
    agent_id: str
    grid_x: int
    grid_z: int
    department: str
    updated_at: datetime

    @classmethod
    def from_domain(cls, value: OfficePlacement) -> OfficePlacementResponse:
        return cls(
            agent_id=value.agent_id,
            grid_x=value.grid_x,
            grid_z=value.grid_z,
            department=value.department,
            updated_at=value.updated_at,
        )


class OfficeObstacleResponse(BaseModel):
    grid_x: int
    grid_z: int
    kind: str


class OfficeLayoutResponse(BaseModel):
    grid: OfficeGridResponse
    rooms: list[OfficeRoomResponse]
    obstacles: list[OfficeObstacleResponse]
    placements: list[OfficePlacementResponse]


class PutOfficePlacementRequest(BaseModel):
    grid_x: int = Field(ge=0, lt=35)
    grid_z: int = Field(ge=0, lt=12)


@router.get(
    "",
    response_model=OfficeLayoutResponse,
    dependencies=[Depends(require_permission(Permission.SYSTEM_INSPECT))],
)
def get_layout(service: ServiceDependency) -> OfficeLayoutResponse:
    return OfficeLayoutResponse(
        grid=OfficeGridResponse(**DEFAULT_OFFICE_GRID.__dict__),
        rooms=[OfficeRoomResponse(**room.__dict__) for room in DEFAULT_OFFICE_ROOMS],
        obstacles=[
            OfficeObstacleResponse(**obstacle.__dict__)
            for obstacle in DEFAULT_OFFICE_OBSTACLES
        ],
        placements=[
            OfficePlacementResponse.from_domain(value)
            for value in service.list_placements()
        ],
    )


@router.put(
    "/placements/{agent_id}",
    response_model=OfficePlacementResponse,
    dependencies=[Depends(require_permission(Permission.TASK_OPERATE))],
)
def put_placement(
    agent_id: str,
    payload: PutOfficePlacementRequest,
    service: ServiceDependency,
) -> OfficePlacementResponse:
    try:
        value = service.place_employee(
            agent_id=agent_id,
            grid_x=payload.grid_x,
            grid_z=payload.grid_z,
        )
    except InvalidOfficePlacement as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except OfficeCellOccupied as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    return OfficePlacementResponse.from_domain(value)
