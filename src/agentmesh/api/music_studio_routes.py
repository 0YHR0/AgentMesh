from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, status
from pydantic import BaseModel, Field

from agentmesh.api.business_object_schemas import BusinessObjectSnapshotResponse
from agentmesh.api.feature_routes import require_feature
from agentmesh.api.schemas import TaskResponse
from agentmesh.api.security import PrincipalDependency, require_permission
from agentmesh.application.music_studio_services import MusicStudioService
from agentmesh.domain.identity import Permission
from agentmesh.features import Feature

router = APIRouter(
    prefix="/api/v1/music-studio",
    tags=["music-studio"],
    dependencies=[
        Depends(require_feature(Feature.COMPANY_PACKS)),
        Depends(require_feature(Feature.ARTIFACT_SERVICE)),
    ],
)


class CreateMusicProjectRequest(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    audience: str = Field(min_length=1, max_length=500)
    language: str = Field(min_length=2, max_length=32)
    mood: str = Field(min_length=1, max_length=160)
    themes: list[str] = Field(min_length=1, max_length=8)
    genre_attributes: list[str] = Field(min_length=1, max_length=8)
    max_rounds: int = Field(default=3, ge=1, le=5)


class MusicProjectLaunchResponse(BaseModel):
    task: TaskResponse
    project: BusinessObjectSnapshotResponse


class RequestMusicRevisionRequest(BaseModel):
    failed_criterion: str = Field(min_length=1, max_length=500)
    requested_change: str = Field(min_length=1, max_length=1000)


class MusicProjectResultResponse(BaseModel):
    task_id: UUID
    status: str
    project_id: UUID
    title: str
    current_round: int
    max_rounds: int
    candidate_id: UUID | None
    review_id: UUID | None
    release_id: UUID | None
    audio_artifact_id: UUID | None
    audio_version_id: UUID | None
    overall_score: int | None
    findings: list[str]
    message: str | None


def get_service(request: Request) -> MusicStudioService:
    return request.app.state.container.music_studio_service


ServiceDependency = Annotated[MusicStudioService, Depends(get_service)]
IdempotencyHeader = Annotated[str, Header(alias="Idempotency-Key", max_length=255)]


@router.post(
    "/projects",
    response_model=MusicProjectLaunchResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        Depends(require_permission(Permission.COMPANY_MANAGE)),
        Depends(require_permission(Permission.TASK_CREATE)),
        Depends(require_permission(Permission.TASK_OPERATE)),
    ],
)
def create_project(
    payload: CreateMusicProjectRequest,
    service: ServiceDependency,
    principal: PrincipalDependency,
    idempotency_key: IdempotencyHeader,
) -> MusicProjectLaunchResponse:
    result = service.launch(
        **payload.model_dump(),
        requested_by=principal.principal_id,
        idempotency_key=idempotency_key,
    )
    return MusicProjectLaunchResponse(
        task=TaskResponse.from_aggregate(result.task),
        project=BusinessObjectSnapshotResponse.from_snapshot(result.project),
    )


@router.get("/projects/{task_id}", response_model=MusicProjectResultResponse)
def get_project(task_id: UUID, service: ServiceDependency) -> MusicProjectResultResponse:
    return MusicProjectResultResponse.model_validate(service.status(task_id), from_attributes=True)


@router.post(
    "/projects/{task_id}/materialize",
    response_model=MusicProjectResultResponse,
    dependencies=[Depends(require_permission(Permission.TASK_OPERATE))],
)
def materialize_project(
    task_id: UUID,
    service: ServiceDependency,
    principal: PrincipalDependency,
) -> MusicProjectResultResponse:
    return MusicProjectResultResponse.model_validate(
        service.materialize(task_id, actor=principal.principal_id), from_attributes=True
    )


@router.post(
    "/projects/{task_id}/approve",
    response_model=MusicProjectResultResponse,
    dependencies=[Depends(require_permission(Permission.COMPANY_MANAGE))],
)
def approve_project(
    task_id: UUID,
    service: ServiceDependency,
    principal: PrincipalDependency,
) -> MusicProjectResultResponse:
    return MusicProjectResultResponse.model_validate(
        service.approve(task_id, actor=principal.principal_id), from_attributes=True
    )


@router.post(
    "/projects/{task_id}/revision",
    response_model=MusicProjectResultResponse,
    dependencies=[Depends(require_permission(Permission.COMPANY_MANAGE))],
)
def request_project_revision(
    task_id: UUID,
    payload: RequestMusicRevisionRequest,
    service: ServiceDependency,
    principal: PrincipalDependency,
    idempotency_key: IdempotencyHeader,
) -> MusicProjectResultResponse:
    return MusicProjectResultResponse.model_validate(
        service.request_revision(
            task_id,
            **payload.model_dump(),
            actor=principal.principal_id,
            idempotency_key=idempotency_key,
        ),
        from_attributes=True,
    )
