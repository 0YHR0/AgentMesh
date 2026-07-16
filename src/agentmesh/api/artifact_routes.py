from __future__ import annotations

import base64
import binascii
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from pydantic import BaseModel, Field

from agentmesh.api.feature_routes import require_feature
from agentmesh.application.artifact_services import ArtifactService
from agentmesh.domain.artifacts import (
    ArtifactAggregate,
    ArtifactClassification,
    ArtifactScanStatus,
    ArtifactStorageClass,
    ArtifactVersion,
    ArtifactVersionStatus,
)
from agentmesh.domain.errors import ArtifactTooLarge, InvalidArtifact
from agentmesh.features import Feature

router = APIRouter(
    prefix="/api/v1",
    tags=["artifacts"],
    dependencies=[Depends(require_feature(Feature.ARTIFACT_SERVICE))],
)


class CreateArtifactRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=255)
    kind: str = Field(min_length=2, max_length=64)
    classification: ArtifactClassification = ArtifactClassification.INTERNAL
    media_type: str = Field(min_length=1, max_length=255)
    content_base64: str = Field(min_length=1)
    expected_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    producer_run_id: UUID | None = None


class CreateArtifactVersionRequest(BaseModel):
    media_type: str = Field(min_length=1, max_length=255)
    content_base64: str = Field(min_length=1)
    expected_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    producer_run_id: UUID | None = None


class ArtifactVersionResponse(BaseModel):
    id: UUID
    artifact_id: UUID
    version_number: int
    media_type: str
    size_bytes: int
    sha256: str
    storage_class: ArtifactStorageClass
    status: ArtifactVersionStatus
    scan_status: ArtifactScanStatus
    producer_run_id: UUID | None
    created_at: datetime

    @classmethod
    def from_domain(cls, value: ArtifactVersion) -> ArtifactVersionResponse:
        return cls(
            id=value.id,
            artifact_id=value.artifact_id,
            version_number=value.version_number,
            media_type=value.media_type,
            size_bytes=value.size_bytes,
            sha256=value.sha256,
            storage_class=value.storage_class,
            status=value.status,
            scan_status=value.scan_status,
            producer_run_id=value.producer_run_id,
            created_at=value.created_at,
        )


class ArtifactResponse(BaseModel):
    id: UUID
    tenant_id: str
    owner_id: str
    display_name: str
    kind: str
    classification: ArtifactClassification
    version_count: int
    revision: int
    created_at: datetime
    updated_at: datetime
    versions: list[ArtifactVersionResponse]

    @classmethod
    def from_aggregate(cls, value: ArtifactAggregate) -> ArtifactResponse:
        return cls(
            id=value.artifact.id,
            tenant_id=value.artifact.tenant_id,
            owner_id=value.artifact.owner_id,
            display_name=value.artifact.display_name,
            kind=value.artifact.kind,
            classification=value.artifact.classification,
            version_count=value.artifact.version_count,
            revision=value.artifact.revision,
            created_at=value.artifact.created_at,
            updated_at=value.artifact.updated_at,
            versions=[ArtifactVersionResponse.from_domain(item) for item in value.versions],
        )


class ArtifactListResponse(BaseModel):
    items: list[ArtifactResponse]
    limit: int
    offset: int


def get_artifact_service(request: Request) -> ArtifactService:
    return request.app.state.container.artifact_service


ArtifactServiceDependency = Annotated[ArtifactService, Depends(get_artifact_service)]
IdempotencyHeader = Annotated[str | None, Header(alias="Idempotency-Key", max_length=255)]
LimitQuery = Annotated[int, Query(ge=1, le=100)]
OffsetQuery = Annotated[int, Query(ge=0)]


@router.post(
    "/artifacts",
    response_model=ArtifactResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_artifact(
    payload: CreateArtifactRequest,
    service: ArtifactServiceDependency,
    idempotency_key: IdempotencyHeader = None,
) -> ArtifactResponse:
    aggregate = service.create_artifact(
        display_name=payload.display_name,
        kind=payload.kind,
        classification=payload.classification,
        media_type=payload.media_type,
        content=_decode_content(payload.content_base64, service.max_inline_bytes),
        expected_sha256=payload.expected_sha256,
        producer_run_id=payload.producer_run_id,
        idempotency_key=idempotency_key,
    )
    return ArtifactResponse.from_aggregate(aggregate)


@router.post(
    "/artifacts/{artifact_id}/versions",
    response_model=ArtifactResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_artifact_version(
    artifact_id: UUID,
    payload: CreateArtifactVersionRequest,
    service: ArtifactServiceDependency,
    idempotency_key: IdempotencyHeader = None,
) -> ArtifactResponse:
    aggregate = service.add_version(
        artifact_id,
        media_type=payload.media_type,
        content=_decode_content(payload.content_base64, service.max_inline_bytes),
        expected_sha256=payload.expected_sha256,
        producer_run_id=payload.producer_run_id,
        idempotency_key=idempotency_key,
    )
    return ArtifactResponse.from_aggregate(aggregate)


@router.get("/artifacts", response_model=ArtifactListResponse)
def list_artifacts(
    service: ArtifactServiceDependency,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
) -> ArtifactListResponse:
    values = service.list_artifacts(limit=limit, offset=offset)
    return ArtifactListResponse(
        items=[ArtifactResponse.from_aggregate(value) for value in values],
        limit=limit,
        offset=offset,
    )


@router.get("/artifacts/{artifact_id}", response_model=ArtifactResponse)
def get_artifact(
    artifact_id: UUID,
    service: ArtifactServiceDependency,
) -> ArtifactResponse:
    return ArtifactResponse.from_aggregate(service.get_artifact(artifact_id))


@router.get("/artifact-versions/{version_id}/content", response_class=Response)
def download_artifact_version(
    version_id: UUID,
    service: ArtifactServiceDependency,
) -> Response:
    artifact, version = service.get_version_content(version_id)
    extension = "json" if version.media_type == "application/json" else "txt"
    digest = base64.b64encode(bytes.fromhex(version.sha256)).decode("ascii")
    filename = f"artifact-{artifact.id}-v{version.version_number}.{extension}"
    return Response(
        content=version.content,
        media_type=version.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Digest": f"sha-256={digest}",
            "ETag": f'"{version.sha256}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


def _decode_content(value: str, max_bytes: int) -> bytes:
    padding = len(value) - len(value.rstrip("="))
    estimated_size = (len(value) * 3) // 4 - padding
    if estimated_size > max_bytes:
        raise ArtifactTooLarge(estimated_size, max_bytes)
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidArtifact("content_base64 must be valid standard Base64") from exc
