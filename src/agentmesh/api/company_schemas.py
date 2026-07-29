from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from agentmesh.application.company_services import CompanySnapshot
from agentmesh.domain.company import (
    Appointment,
    AppointmentStatus,
    Company,
    CompanyStatus,
    OrganizationNodeType,
    OrganizationRelationship,
    OrganizationUnit,
    Position,
    ResourceStatus,
)


class CreateCompanyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    mission: str = Field(min_length=1, max_length=10_000)
    risk_policy_id: UUID | None = None
    default_currency: str = Field(default="USD", min_length=3, max_length=3)
    operating_timezone: str = Field(default="UTC", min_length=1, max_length=64)


class CreateOrganizationUnitRequest(BaseModel):
    key: str = Field(min_length=2, max_length=63)
    name: str = Field(min_length=1, max_length=160)
    kind: str = Field(default="department", min_length=2, max_length=63)
    purpose: str = Field(min_length=1, max_length=10_000)
    parent_unit_id: UUID | None = None
    budget_policy_id: UUID | None = None
    memory_namespace: str | None = Field(default=None, max_length=255)


class CreatePositionRequest(BaseModel):
    primary_unit_id: UUID
    key: str = Field(min_length=2, max_length=63)
    title: str = Field(min_length=1, max_length=160)
    responsibility_contract: dict[str, Any]
    required_capabilities: list[str] = Field(default_factory=list, max_length=200)
    allowed_tool_capabilities: list[str] = Field(default_factory=list, max_length=200)
    memory_policy_id: UUID | None = None
    approval_scope: dict[str, Any] = Field(default_factory=dict)
    budget_scope: dict[str, Any] = Field(default_factory=dict)
    reports_to_position_id: UUID | None = None


class CreateAppointmentRequest(BaseModel):
    position_id: UUID
    agent_definition_id: UUID
    agent_version_id: UUID
    reason: str = Field(min_length=1, max_length=2_000)


class CreateOrganizationRelationshipRequest(BaseModel):
    relationship_type: str = Field(min_length=2, max_length=63)
    source_type: OrganizationNodeType
    source_id: UUID
    target_type: OrganizationNodeType
    target_id: UUID
    attributes: dict[str, Any] = Field(default_factory=dict)


class CompanyResponse(BaseModel):
    id: UUID
    tenant_id: str
    name: str
    mission: str
    owner_principal_id: str
    status: CompanyStatus
    risk_policy_id: UUID | None
    default_currency: str
    operating_timezone: str
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, value: Company) -> "CompanyResponse":
        return cls(**value.__dict__)


class OrganizationUnitResponse(BaseModel):
    id: UUID
    company_id: UUID
    key: str
    name: str
    kind: str
    purpose: str
    parent_unit_id: UUID | None
    budget_policy_id: UUID | None
    memory_namespace: str | None
    status: ResourceStatus
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, value: OrganizationUnit) -> "OrganizationUnitResponse":
        return cls(**value.__dict__)


class PositionResponse(BaseModel):
    id: UUID
    company_id: UUID
    primary_unit_id: UUID
    key: str
    title: str
    responsibility_contract: dict[str, Any]
    required_capabilities: list[str]
    allowed_tool_capabilities: list[str]
    memory_policy_id: UUID | None
    approval_scope: dict[str, Any]
    budget_scope: dict[str, Any]
    reports_to_position_id: UUID | None
    status: ResourceStatus
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, value: Position) -> "PositionResponse":
        return cls(
            **{
                **value.__dict__,
                "required_capabilities": list(value.required_capabilities),
                "allowed_tool_capabilities": list(value.allowed_tool_capabilities),
            }
        )


class AppointmentResponse(BaseModel):
    id: UUID
    company_id: UUID
    position_id: UUID
    agent_definition_id: UUID
    agent_version_id: UUID
    starts_at: datetime
    ends_at: datetime | None
    appointed_by: str
    reason: str
    status: AppointmentStatus
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, value: Appointment) -> "AppointmentResponse":
        return cls(**value.__dict__)


class OrganizationRelationshipResponse(BaseModel):
    id: UUID
    company_id: UUID
    relationship_type: str
    source_type: OrganizationNodeType
    source_id: UUID
    target_type: OrganizationNodeType
    target_id: UUID
    attributes: dict[str, Any]
    status: ResourceStatus
    created_at: datetime
    ended_at: datetime | None

    @classmethod
    def from_domain(
        cls, value: OrganizationRelationship
    ) -> "OrganizationRelationshipResponse":
        return cls(**value.__dict__)


class CompanySnapshotResponse(BaseModel):
    company: CompanyResponse
    units: list[OrganizationUnitResponse]
    positions: list[PositionResponse]
    appointments: list[AppointmentResponse]
    relationships: list[OrganizationRelationshipResponse]

    @classmethod
    def from_snapshot(cls, value: CompanySnapshot) -> "CompanySnapshotResponse":
        return cls(
            company=CompanyResponse.from_domain(value.company),
            units=[OrganizationUnitResponse.from_domain(item) for item in value.units],
            positions=[PositionResponse.from_domain(item) for item in value.positions],
            appointments=[AppointmentResponse.from_domain(item) for item in value.appointments],
            relationships=[
                OrganizationRelationshipResponse.from_domain(item)
                for item in value.relationships
            ],
        )
