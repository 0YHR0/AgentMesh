from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agentmesh.api.business_object_schemas import BusinessObjectSnapshotResponse
from agentmesh.api.company_schemas import AppointmentResponse, CompanyResponse
from agentmesh.api.schemas import TaskResponse
from agentmesh.application.company_pack_services import (
    CompanyOperationsPreview,
    CompanyTemplateInstallation,
    CompanyTemplatePreview,
    CompanyWorkforcePreview,
    PackPreview,
    PackUpgradePreview,
    PackUpgradeResult,
)
from agentmesh.application.market_research_services import MarketResearchPreflight
from agentmesh.application.research_materialization_services import ResearchMaterialization
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
    revision: int
    upgraded_by: str | None
    upgraded_at: datetime | None


class PackUpgradePreviewResponse(BaseModel):
    company_id: UUID
    installation_id: UUID
    target_pack_id: UUID
    pack_key: str
    from_version: str
    from_digest: str
    to_version: str
    to_digest: str
    resource_changes: list[dict[str, Any]]
    blockers: list[str]
    warnings: list[str]
    affected_object_count: int
    upgradeable: bool

    @classmethod
    def from_domain(cls, value: PackUpgradePreview) -> "PackUpgradePreviewResponse":
        return cls(**value.__dict__)


class UpgradePackRequest(BaseModel):
    expected_from_digest: str = Field(min_length=64, max_length=64)
    expected_target_digest: str = Field(min_length=64, max_length=64)


class PackUpgradeRecordResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    company_id: UUID
    installation_id: UUID
    pack_key: str
    from_version: str
    from_digest: str
    to_version: str
    to_digest: str
    upgraded_by: str
    resource_changes: list[dict[str, Any]]
    migrated_object_count: int
    created_at: datetime


class PackUpgradeResultResponse(BaseModel):
    installation: PackInstallationResponse
    upgrade: PackUpgradeRecordResponse

    @classmethod
    def from_domain(cls, value: PackUpgradeResult) -> "PackUpgradeResultResponse":
        return cls(
            installation=PackInstallationResponse.model_validate(value.installation),
            upgrade=PackUpgradeRecordResponse.model_validate(value.upgrade),
        )


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
    installed_version: str | None
    upgrade_available: bool
    installable: bool

    @classmethod
    def from_domain(cls, value: CompanyTemplatePreview) -> "CompanyTemplatePreviewResponse":
        return cls(**value.__dict__)


class CompanyOperationsPreviewResponse(BaseModel):
    name: str
    version: str
    content_digest: str
    active_company_id: UUID | None
    base_pack_installed: bool
    already_installed: bool
    required_features: list[str]
    missing_features: list[str]
    resource_summary: dict[str, int]
    resources: list[dict[str, str]]
    operations_start_in_draft: bool
    external_writes_enabled: bool
    installable: bool

    @classmethod
    def from_domain(cls, value: CompanyOperationsPreview) -> "CompanyOperationsPreviewResponse":
        return cls(**value.__dict__)


class ActivateMarketIntelligenceOperationsRequest(BaseModel):
    starts_at: datetime
    cycle_days: int = Field(default=28, ge=7, le=365)
    budget_limit_micros: int = Field(default=10_000_000, ge=1)
    currency: str | None = Field(default=None, min_length=3, max_length=3)


class CompanyWorkforcePreviewResponse(BaseModel):
    active_company_id: UUID | None
    operations_pack_installed: bool
    missing_features: list[str]
    positions: list[dict[str, Any]]
    operations: list[dict[str, Any]]
    fully_staffed: bool
    activatable_operation_count: int

    @classmethod
    def from_domain(
        cls, value: CompanyWorkforcePreview
    ) -> "CompanyWorkforcePreviewResponse":
        return cls(**value.__dict__)


class WorkforceAssignmentRequest(BaseModel):
    position_key: str = Field(min_length=1, max_length=63)
    agent_version_id: UUID


class AppointMarketIntelligenceWorkforceRequest(BaseModel):
    assignments: list[WorkforceAssignmentRequest] = Field(
        min_length=1, max_length=17
    )
    reason: str = Field(
        default="Staff the Market Intelligence operating team.",
        min_length=1,
        max_length=2_000,
    )


class CompanyWorkforceAppointmentsResponse(BaseModel):
    appointments: list[AppointmentResponse]


class MarketResearchPreflightResponse(BaseModel):
    company_id: UUID | None
    ready: bool
    blockers: list[dict[str, str]]
    warnings: list[dict[str, str]]
    tools: list[dict[str, Any]]
    positions: list[dict[str, Any]]
    output_contract: list[str]
    external_writes_enabled: bool

    @classmethod
    def from_domain(
        cls, value: MarketResearchPreflight
    ) -> "MarketResearchPreflightResponse":
        return cls(**value.__dict__)


class LaunchMarketResearchRequest(BaseModel):
    question: str = Field(min_length=10, max_length=2_000)
    target_audience: str = Field(min_length=1, max_length=500)
    decision_supported: str = Field(min_length=1, max_length=1_000)
    scope: str = Field(min_length=1, max_length=4_000)
    max_sources: int = Field(default=12, ge=3, le=50)


class MarketResearchLaunchResponse(BaseModel):
    task: TaskResponse
    research_question: BusinessObjectSnapshotResponse
    preflight: MarketResearchPreflightResponse


class ResearchMaterializationResponse(BaseModel):
    task_id: UUID
    status: str
    source_record_ids: list[UUID]
    claim_register_ids: list[UUID]
    report_id: UUID | None
    artifact_id: UUID | None
    message: str | None

    @classmethod
    def from_domain(
        cls, value: ResearchMaterialization
    ) -> "ResearchMaterializationResponse":
        return cls(**value.__dict__)


class InstallMarketIntelligenceTemplateRequest(BaseModel):
    company_name: str = Field(
        default="AgentMesh Market Intelligence Studio",
        min_length=1,
        max_length=160,
    )
    mission: str = Field(
        default=("Turn verified market evidence into useful, trustworthy business intelligence."),
        min_length=1,
        max_length=10_000,
    )
    target_market: str = Field(min_length=1, max_length=500)
    product_type: Literal["research-report", "subscription", "custom-research"] = "research-report"
    excluded_sectors: list[str] = Field(default_factory=list, max_length=20)
    default_currency: str = Field(default="USD", min_length=3, max_length=3)
    operating_timezone: str = Field(default="UTC", min_length=1, max_length=64)


class InstallMusicStudioTemplateRequest(BaseModel):
    company_name: str = Field(
        default="AgentMesh Music Studio",
        min_length=1,
        max_length=160,
    )
    mission: str = Field(
        default="Turn creative intent into original, reviewed, traceable music.",
        min_length=1,
        max_length=10_000,
    )
    default_language: str = Field(default="en", min_length=2, max_length=32)
    default_genre: str = Field(default="pop", min_length=1, max_length=120)
    use_plan: Literal["internal-demo", "personal", "commercial-review"] = "internal-demo"
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
