from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agentmesh.application.company_pack_services import PackPreview
from agentmesh.domain.company_packs import PackKind, PackStatus


class CreatePackRequest(BaseModel):
    key: str = Field(min_length=1, max_length=63)
    version: str = Field(min_length=5, max_length=32)
    name: str = Field(min_length=1, max_length=160)
    kind: PackKind
    manifest: dict[str, Any]
    required_features: list[str] = Field(default_factory=list, max_length=32)
    dependencies: list[str] = Field(default_factory=list, max_length=32)


class PackResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    key: str
    version: str
    name: str
    kind: PackKind
    manifest: dict[str, Any]
    required_features: list[str]
    dependencies: list[str]
    content_digest: str
    status: PackStatus
    created_at: datetime
    published_at: datetime | None


class PackPreviewResponse(BaseModel):
    pack_id: UUID
    content_digest: str
    required_features: list[str]
    missing_features: list[str]
    missing_dependencies: list[str]
    resources: list[dict[str, str]]
    installable: bool

    @classmethod
    def from_domain(cls, value: PackPreview) -> "PackPreviewResponse":
        return cls(**value.__dict__)


class InstallPackRequest(BaseModel):
    expected_digest: str = Field(min_length=64, max_length=64)


class PackInstallationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    company_id: UUID
    pack_id: UUID
    pack_key: str
    pack_version: str
    pack_digest: str
    installed_by: str
    resource_refs: list[dict[str, str]]
    installed_at: datetime
