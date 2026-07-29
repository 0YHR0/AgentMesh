from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agentmesh.application.financial_governance_services import (
    AllocationBalance,
    FinanceDashboard,
)
from agentmesh.domain.financial_governance import (
    AllocationScope,
    AllocationStatus,
    BudgetEntryType,
    EconomicEvidence,
    EconomicEvidenceKind,
    EvidenceVerification,
    ExpenseStatus,
)


class CreateAllocationRequest(BaseModel):
    scope_type: AllocationScope
    scope_id: str = Field(min_length=1, max_length=255)
    currency: str = Field(min_length=3, max_length=3)
    approved_limit_micros: int = Field(gt=0)
    policy_version: int = Field(ge=1)
    parent_allocation_id: UUID | None = None


class AllocationResponse(BaseModel):
    id: UUID
    company_id: UUID
    parent_allocation_id: UUID | None
    scope_type: AllocationScope
    scope_id: str
    currency: str
    approved_limit_micros: int
    policy_version: int
    status: AllocationStatus
    created_at: datetime
    closed_at: datetime | None
    reserved_micros: int
    settled_micros: int
    available_micros: int

    @classmethod
    def from_balance(cls, value: AllocationBalance) -> "AllocationResponse":
        return cls(
            **value.allocation.__dict__,
            reserved_micros=value.reserved_micros,
            settled_micros=value.settled_micros,
            available_micros=value.available_micros,
        )


class BudgetEntryRequest(BaseModel):
    amount_micros: int = Field(gt=0)
    operation_key: str = Field(min_length=1, max_length=128)
    task_id: UUID | None = None
    evidence_ref: str | None = Field(default=None, max_length=255)


class BudgetEntryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    allocation_id: UUID
    entry_type: BudgetEntryType
    amount_micros: int
    operation_key: str
    task_id: UUID | None
    evidence_ref: str | None
    actor: str
    created_at: datetime


class RecordEconomicEvidenceRequest(BaseModel):
    kind: EconomicEvidenceKind
    verification: EvidenceVerification
    amount_micros: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    external_ref: str | None = Field(default=None, max_length=255)
    source_snapshot_digest: str | None = Field(default=None, max_length=64)
    organization_unit_id: UUID | None = None
    initiative_id: UUID | None = None
    operation_id: UUID | None = None
    task_id: UUID | None = None
    attribution_method: str = Field(min_length=1, max_length=63)
    occurred_at: datetime


class EconomicEvidenceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    company_id: UUID
    kind: EconomicEvidenceKind
    verification: EvidenceVerification
    amount_micros: int
    currency: str
    external_ref: str | None
    source_snapshot_digest: str | None
    organization_unit_id: UUID | None
    initiative_id: UUID | None
    operation_id: UUID | None
    task_id: UUID | None
    attribution_method: str
    recorded_by: str
    occurred_at: datetime
    created_at: datetime
    evidence_digest: str

    @classmethod
    def from_domain(cls, value: EconomicEvidence) -> "EconomicEvidenceResponse":
        return cls(**value.__dict__, evidence_digest=value.evidence_digest)


class ProposeExpenseRequest(BaseModel):
    allocation_id: UUID
    purpose: str = Field(min_length=1, max_length=500)
    vendor_ref: str = Field(min_length=1, max_length=255)
    amount_micros: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    risk_tier: str = Field(min_length=1, max_length=32)
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)


class ReviewExpenseRequest(BaseModel):
    approved: bool
    reason: str = Field(min_length=1, max_length=500)


class ExpenseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    company_id: UUID
    allocation_id: UUID
    requested_by: str
    purpose: str
    vendor_ref: str
    amount_micros: int
    currency: str
    risk_tier: str
    evidence_refs: list[str]
    status: ExpenseStatus
    reviewed_by: str | None
    review_reason: str | None
    created_at: datetime
    reviewed_at: datetime | None


class FinanceDashboardResponse(BaseModel):
    currency: str
    estimated_pipeline_micros: int
    estimated_offers_micros: int
    contracted_revenue_micros: int
    invoiced_revenue_micros: int
    collected_cash_micros: int
    settled_cost_micros: int
    verified_margin_micros: int
    active_reserved_micros: int
    open_expense_requests: int

    @classmethod
    def from_domain(cls, value: FinanceDashboard) -> "FinanceDashboardResponse":
        return cls(**value.__dict__)
