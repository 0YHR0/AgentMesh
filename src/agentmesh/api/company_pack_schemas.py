from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agentmesh.api.company_schemas import CompanyResponse
from agentmesh.application.company_pack_services import (
    CompanyTemplateInstallation,
    CompanyTemplatePreview,
    PackPreview,
)
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
    configuration: dict[str, Any] = Field(default_factory=dict)


class PackInstallationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    company_id: UUID
    pack_id: UUID
    pack_key: str
    pack_version: str
    pack_digest: str
    installed_by: str
    configuration: dict[str, Any]
    resource_refs: list[dict[str, str]]
    installed_at: datetime


class CompanyTemplatePreviewResponse(BaseModel):
    slug: str
    name: str
    version: str
    mission: str
    content_digest: str
    required_features: list[str]
    missing_features: list[str]
    resource_summary: dict[str, int]
    resources: list[dict[str, str]]
    required_credentials: list[str]
    permissions: list[str]
    external_writes_enabled: bool
    active_company_id: UUID | None
    installable: bool

    @classmethod
    def from_domain(
        cls, value: CompanyTemplatePreview
    ) -> "CompanyTemplatePreviewResponse":
        return cls(**value.__dict__)


class InstallMarketIntelligenceTemplateRequest(BaseModel):
    company_name: str = Field(
        default="AgentMesh Market Intelligence Studio",
        min_length=1,
        max_length=160,
    )
    mission: str = Field(
        default=(
            "Turn verified market evidence into useful, trustworthy "
            "business intelligence."
        ),
        min_length=1,
        max_length=10_000,
    )
    target_market: str = Field(min_length=1, max_length=500)
    product_type: Literal[
        "research-report", "subscription", "custom-research"
    ] = "research-report"
    excluded_sectors: list[str] = Field(default_factory=list, max_length=20)
    default_currency: str = Field(default="USD", min_length=3, max_length=3)
    operating_timezone: str = Field(default="UTC", min_length=1, max_length=64)


class CompanyTemplateInstallationResponse(BaseModel):
    company: CompanyResponse
    installation: PackInstallationResponse

    @classmethod
    def from_domain(
        cls, value: CompanyTemplateInstallation
    ) -> "CompanyTemplateInstallationResponse":
        return cls(
            company=CompanyResponse.from_domain(value.company),
            installation=PackInstallationResponse.model_validate(value.installation),
        )
