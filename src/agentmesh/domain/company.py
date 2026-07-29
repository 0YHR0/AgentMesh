from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from agentmesh.domain.errors import InvalidCompanyModel
from agentmesh.domain.tasks import utc_now

RESOURCE_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,62}$")
RELATIONSHIP_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{1,62}$")
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")


class CompanyStatus(str, Enum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class ResourceStatus(str, Enum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class AppointmentStatus(str, Enum):
    ACTIVE = "ACTIVE"
    ENDED = "ENDED"


class OrganizationNodeType(str, Enum):
    UNIT = "UNIT"
    POSITION = "POSITION"


def _required(value: str, label: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise InvalidCompanyModel(f"{label} is required")
    if len(normalized) > maximum:
        raise InvalidCompanyModel(f"{label} must not exceed {maximum} characters")
    return normalized


def normalize_resource_key(value: str, label: str = "Resource key") -> str:
    normalized = value.strip().lower()
    if not RESOURCE_KEY_PATTERN.fullmatch(normalized):
        raise InvalidCompanyModel(
            f"{label} must be 2-63 lowercase letters, numbers, or hyphens"
        )
    return normalized


@dataclass
class Company:
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
    def create(
        cls,
        *,
        tenant_id: str,
        name: str,
        mission: str,
        owner_principal_id: str,
        risk_policy_id: UUID | None = None,
        default_currency: str = "USD",
        operating_timezone: str = "UTC",
    ) -> Company:
        currency = default_currency.strip().upper()
        if not CURRENCY_PATTERN.fullmatch(currency):
            raise InvalidCompanyModel("Default currency must be a three-letter ISO-style code")
        now = utc_now()
        return cls(
            id=uuid4(),
            tenant_id=_required(tenant_id, "Tenant ID", 128),
            name=_required(name, "Company name", 160),
            mission=_required(mission, "Company mission", 10_000),
            owner_principal_id=_required(owner_principal_id, "Owner principal ID", 128),
            status=CompanyStatus.ACTIVE,
            risk_policy_id=risk_policy_id,
            default_currency=currency,
            operating_timezone=_required(operating_timezone, "Operating timezone", 64),
            version=1,
            created_at=now,
            updated_at=now,
        )

    def archive(self) -> None:
        if self.status is CompanyStatus.ARCHIVED:
            raise InvalidCompanyModel("Company is already archived")
        self.status = CompanyStatus.ARCHIVED
        self.version += 1
        self.updated_at = utc_now()


@dataclass
class OrganizationUnit:
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
    def create(
        cls,
        *,
        company_id: UUID,
        key: str,
        name: str,
        kind: str,
        purpose: str,
        parent_unit_id: UUID | None = None,
        budget_policy_id: UUID | None = None,
        memory_namespace: str | None = None,
    ) -> OrganizationUnit:
        now = utc_now()
        namespace = memory_namespace.strip() if memory_namespace else None
        if namespace and len(namespace) > 255:
            raise InvalidCompanyModel("Memory namespace must not exceed 255 characters")
        return cls(
            id=uuid4(),
            company_id=company_id,
            key=normalize_resource_key(key, "Organization Unit key"),
            name=_required(name, "Organization Unit name", 160),
            kind=normalize_resource_key(kind, "Organization Unit kind"),
            purpose=_required(purpose, "Organization Unit purpose", 10_000),
            parent_unit_id=parent_unit_id,
            budget_policy_id=budget_policy_id,
            memory_namespace=namespace,
            status=ResourceStatus.ACTIVE,
            version=1,
            created_at=now,
            updated_at=now,
        )


@dataclass
class Position:
    id: UUID
    company_id: UUID
    primary_unit_id: UUID
    key: str
    title: str
    responsibility_contract: dict[str, Any]
    required_capabilities: tuple[str, ...]
    allowed_tool_capabilities: tuple[str, ...]
    memory_policy_id: UUID | None
    approval_scope: dict[str, Any]
    budget_scope: dict[str, Any]
    reports_to_position_id: UUID | None
    status: ResourceStatus
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def create(
        cls,
        *,
        company_id: UUID,
        primary_unit_id: UUID,
        key: str,
        title: str,
        responsibility_contract: dict[str, Any],
        required_capabilities: list[str] | tuple[str, ...] = (),
        allowed_tool_capabilities: list[str] | tuple[str, ...] = (),
        memory_policy_id: UUID | None = None,
        approval_scope: dict[str, Any] | None = None,
        budget_scope: dict[str, Any] | None = None,
        reports_to_position_id: UUID | None = None,
    ) -> Position:
        contract = dict(responsibility_contract)
        if not contract:
            raise InvalidCompanyModel("Position responsibility contract is required")
        now = utc_now()
        return cls(
            id=uuid4(),
            company_id=company_id,
            primary_unit_id=primary_unit_id,
            key=normalize_resource_key(key, "Position key"),
            title=_required(title, "Position title", 160),
            responsibility_contract=contract,
            required_capabilities=tuple(sorted(set(required_capabilities))),
            allowed_tool_capabilities=tuple(sorted(set(allowed_tool_capabilities))),
            memory_policy_id=memory_policy_id,
            approval_scope=dict(approval_scope or {}),
            budget_scope=dict(budget_scope or {}),
            reports_to_position_id=reports_to_position_id,
            status=ResourceStatus.ACTIVE,
            version=1,
            created_at=now,
            updated_at=now,
        )


@dataclass
class Appointment:
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
    def create(
        cls,
        *,
        company_id: UUID,
        position_id: UUID,
        agent_definition_id: UUID,
        agent_version_id: UUID,
        appointed_by: str,
        reason: str,
    ) -> Appointment:
        now = utc_now()
        return cls(
            id=uuid4(),
            company_id=company_id,
            position_id=position_id,
            agent_definition_id=agent_definition_id,
            agent_version_id=agent_version_id,
            starts_at=now,
            ends_at=None,
            appointed_by=_required(appointed_by, "Appointing principal", 128),
            reason=_required(reason, "Appointment reason", 2_000),
            status=AppointmentStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )

    def end(self) -> None:
        if self.status is AppointmentStatus.ENDED:
            raise InvalidCompanyModel("Appointment has already ended")
        now = utc_now()
        self.status = AppointmentStatus.ENDED
        self.ends_at = now
        self.updated_at = now


@dataclass
class OrganizationRelationship:
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
    def create(
        cls,
        *,
        company_id: UUID,
        relationship_type: str,
        source_type: OrganizationNodeType,
        source_id: UUID,
        target_type: OrganizationNodeType,
        target_id: UUID,
        attributes: dict[str, Any] | None = None,
    ) -> OrganizationRelationship:
        relation = relationship_type.strip().lower()
        if not RELATIONSHIP_KEY_PATTERN.fullmatch(relation):
            raise InvalidCompanyModel(
                "Relationship type must be a 2-63 character lowercase namespaced key"
            )
        if source_type is target_type and source_id == target_id:
            raise InvalidCompanyModel("Organization relationship cannot point to itself")
        return cls(
            id=uuid4(),
            company_id=company_id,
            relationship_type=relation,
            source_type=source_type,
            source_id=source_id,
            target_type=target_type,
            target_id=target_id,
            attributes=dict(attributes or {}),
            status=ResourceStatus.ACTIVE,
            created_at=utc_now(),
            ended_at=None,
        )
