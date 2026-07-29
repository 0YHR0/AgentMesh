from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
from uuid import UUID, uuid4

from agentmesh.domain.errors import InvalidFinancialRecord
from agentmesh.domain.tasks import utc_now

CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")


class AllocationScope(str, Enum):
    COMPANY = "COMPANY"
    OPERATING_CYCLE = "OPERATING_CYCLE"
    ORGANIZATION_UNIT = "ORGANIZATION_UNIT"
    INITIATIVE = "INITIATIVE"
    OPERATION = "OPERATION"


class AllocationStatus(str, Enum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"


class BudgetEntryType(str, Enum):
    RESERVE = "RESERVE"
    RELEASE = "RELEASE"
    SETTLE = "SETTLE"


class EconomicEvidenceKind(str, Enum):
    OPPORTUNITY = "OPPORTUNITY"
    OFFER = "OFFER"
    CONTRACTED_REVENUE = "CONTRACTED_REVENUE"
    INVOICED_REVENUE = "INVOICED_REVENUE"
    COLLECTED_CASH = "COLLECTED_CASH"
    SETTLED_COST = "SETTLED_COST"


class EvidenceVerification(str, Enum):
    ESTIMATED = "ESTIMATED"
    VERIFIED = "VERIFIED"


class ExpenseStatus(str, Enum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELED = "CANCELED"


def _required(value: str, label: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise InvalidFinancialRecord(f"{label} is required")
    if len(normalized) > maximum:
        raise InvalidFinancialRecord(f"{label} must not exceed {maximum} characters")
    return normalized


def normalize_currency(value: str) -> str:
    normalized = value.strip().upper()
    if not CURRENCY_PATTERN.fullmatch(normalized):
        raise InvalidFinancialRecord("Currency must be a three-letter ISO-style code")
    return normalized


def positive_amount(value: int, label: str = "Amount") -> int:
    if value <= 0:
        raise InvalidFinancialRecord(f"{label} must be positive integer micros")
    return value


@dataclass
class BudgetAllocation:
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

    @classmethod
    def create(
        cls,
        *,
        company_id: UUID,
        scope_type: AllocationScope,
        scope_id: str,
        currency: str,
        approved_limit_micros: int,
        policy_version: int,
        parent_allocation_id: UUID | None = None,
    ) -> BudgetAllocation:
        if policy_version < 1:
            raise InvalidFinancialRecord("Budget policy version must be positive")
        return cls(
            id=uuid4(),
            company_id=company_id,
            parent_allocation_id=parent_allocation_id,
            scope_type=scope_type,
            scope_id=_required(scope_id, "Budget scope ID", 255),
            currency=normalize_currency(currency),
            approved_limit_micros=positive_amount(
                approved_limit_micros, "Approved limit"
            ),
            policy_version=policy_version,
            status=AllocationStatus.ACTIVE,
            created_at=utc_now(),
            closed_at=None,
        )

    def close(self) -> None:
        if self.status is not AllocationStatus.ACTIVE:
            raise InvalidFinancialRecord("Only an active Budget Allocation can be closed")
        self.status = AllocationStatus.CLOSED
        self.closed_at = utc_now()


@dataclass(frozen=True)
class BudgetLedgerEntry:
    id: UUID
    allocation_id: UUID
    entry_type: BudgetEntryType
    amount_micros: int
    operation_key: str
    task_id: UUID | None
    evidence_ref: str | None
    actor: str
    created_at: datetime

    @classmethod
    def create(
        cls,
        *,
        allocation_id: UUID,
        entry_type: BudgetEntryType,
        amount_micros: int,
        operation_key: str,
        actor: str,
        task_id: UUID | None = None,
        evidence_ref: str | None = None,
    ) -> BudgetLedgerEntry:
        return cls(
            id=uuid4(),
            allocation_id=allocation_id,
            entry_type=entry_type,
            amount_micros=positive_amount(amount_micros),
            operation_key=_required(operation_key, "Budget operation key", 128),
            task_id=task_id,
            evidence_ref=(
                _required(evidence_ref, "Budget evidence reference", 255)
                if evidence_ref
                else None
            ),
            actor=_required(actor, "Budget actor", 128),
            created_at=utc_now(),
        )


@dataclass(frozen=True)
class EconomicEvidence:
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

    @classmethod
    def create(
        cls,
        *,
        company_id: UUID,
        kind: EconomicEvidenceKind,
        verification: EvidenceVerification,
        amount_micros: int,
        currency: str,
        attribution_method: str,
        recorded_by: str,
        occurred_at: datetime,
        external_ref: str | None = None,
        source_snapshot_digest: str | None = None,
        organization_unit_id: UUID | None = None,
        initiative_id: UUID | None = None,
        operation_id: UUID | None = None,
        task_id: UUID | None = None,
    ) -> EconomicEvidence:
        if occurred_at.tzinfo is None:
            raise InvalidFinancialRecord("Economic evidence time must include a timezone")
        verified_only = {
            EconomicEvidenceKind.CONTRACTED_REVENUE,
            EconomicEvidenceKind.INVOICED_REVENUE,
            EconomicEvidenceKind.COLLECTED_CASH,
            EconomicEvidenceKind.SETTLED_COST,
        }
        if kind in verified_only and verification is not EvidenceVerification.VERIFIED:
            raise InvalidFinancialRecord(f"{kind.value} must use VERIFIED evidence")
        if verification is EvidenceVerification.VERIFIED:
            if not external_ref or not source_snapshot_digest:
                raise InvalidFinancialRecord(
                    "Verified evidence requires an external reference and snapshot digest"
                )
            digest = source_snapshot_digest.strip().lower()
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise InvalidFinancialRecord(
                    "Source snapshot digest must be a SHA-256 hex digest"
                )
        else:
            digest = None
        return cls(
            id=uuid4(),
            company_id=company_id,
            kind=kind,
            verification=verification,
            amount_micros=positive_amount(amount_micros),
            currency=normalize_currency(currency),
            external_ref=external_ref.strip() if external_ref else None,
            source_snapshot_digest=digest,
            organization_unit_id=organization_unit_id,
            initiative_id=initiative_id,
            operation_id=operation_id,
            task_id=task_id,
            attribution_method=_required(
                attribution_method, "Attribution method", 63
            ).upper(),
            recorded_by=_required(recorded_by, "Evidence recorder", 128),
            occurred_at=occurred_at,
            created_at=utc_now(),
        )

    @property
    def evidence_digest(self) -> str:
        canonical = "|".join(
            (
                str(self.company_id),
                self.kind.value,
                self.verification.value,
                str(self.amount_micros),
                self.currency,
                self.external_ref or "",
                self.source_snapshot_digest or "",
                self.occurred_at.isoformat(),
            )
        )
        return sha256(canonical.encode()).hexdigest()


@dataclass
class ExpenseRequest:
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

    @classmethod
    def propose(
        cls,
        *,
        company_id: UUID,
        allocation_id: UUID,
        requested_by: str,
        purpose: str,
        vendor_ref: str,
        amount_micros: int,
        currency: str,
        risk_tier: str,
        evidence_refs: list[str],
    ) -> ExpenseRequest:
        tier = _required(risk_tier, "Risk tier", 32).upper()
        if tier not in {"R1_INTERNAL", "R2_EXTERNAL_LOW", "R3_COMMITMENT"}:
            raise InvalidFinancialRecord(
                "Expense requests support R1_INTERNAL through R3_COMMITMENT"
            )
        return cls(
            id=uuid4(),
            company_id=company_id,
            allocation_id=allocation_id,
            requested_by=_required(requested_by, "Expense requester", 128),
            purpose=_required(purpose, "Expense purpose", 500),
            vendor_ref=_required(vendor_ref, "Vendor reference", 255),
            amount_micros=positive_amount(amount_micros),
            currency=normalize_currency(currency),
            risk_tier=tier,
            evidence_refs=sorted(
                {_required(item, "Expense evidence reference", 255) for item in evidence_refs}
            ),
            status=ExpenseStatus.PROPOSED,
            reviewed_by=None,
            review_reason=None,
            created_at=utc_now(),
            reviewed_at=None,
        )

    def review(self, *, approved: bool, reviewer: str, reason: str) -> None:
        normalized = _required(reviewer, "Expense reviewer", 128)
        if self.status is not ExpenseStatus.PROPOSED:
            raise InvalidFinancialRecord("Only a proposed Expense Request can be reviewed")
        if normalized == self.requested_by:
            raise InvalidFinancialRecord("Expense requester cannot approve their own request")
        self.status = ExpenseStatus.APPROVED if approved else ExpenseStatus.REJECTED
        self.reviewed_by = normalized
        self.review_reason = _required(reason, "Expense review reason", 500)
        self.reviewed_at = utc_now()
