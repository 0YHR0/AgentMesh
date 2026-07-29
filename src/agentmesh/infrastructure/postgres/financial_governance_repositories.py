from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentmesh.domain.financial_governance import (
    AllocationScope,
    AllocationStatus,
    BudgetAllocation,
    BudgetEntryType,
    BudgetLedgerEntry,
    EconomicEvidence,
    EconomicEvidenceKind,
    EvidenceVerification,
    ExpenseRequest,
    ExpenseStatus,
)
from agentmesh.infrastructure.postgres.models import (
    BudgetAllocationRecord,
    BudgetLedgerEntryRecord,
    EconomicEvidenceRecord,
    ExpenseRequestRecord,
)


class SqlAlchemyFinancialGovernanceRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_allocation(self, value: BudgetAllocation) -> None:
        self._session.add(
            BudgetAllocationRecord(**self._allocation_data(value))
        )

    def get_allocation(
        self, allocation_id: UUID, *, for_update: bool = False
    ) -> BudgetAllocation | None:
        record = self._session.get(
            BudgetAllocationRecord, allocation_id, with_for_update=for_update
        )
        return self._allocation(record) if record else None

    def list_allocations(self, company_id: UUID) -> list[BudgetAllocation]:
        records = self._session.scalars(
            select(BudgetAllocationRecord)
            .where(BudgetAllocationRecord.company_id == company_id)
            .order_by(BudgetAllocationRecord.created_at)
        )
        return [self._allocation(record) for record in records]

    def save_allocation(self, value: BudgetAllocation) -> None:
        record = self._required(BudgetAllocationRecord, value.id)
        record.status = value.status.value
        record.closed_at = value.closed_at

    def add_ledger_entry(self, value: BudgetLedgerEntry) -> None:
        data = dict(value.__dict__)
        data["entry_type"] = value.entry_type.value
        self._session.add(BudgetLedgerEntryRecord(**data))

    def get_ledger_entry_by_key(
        self, allocation_id: UUID, operation_key: str
    ) -> BudgetLedgerEntry | None:
        record = self._session.scalar(
            select(BudgetLedgerEntryRecord).where(
                BudgetLedgerEntryRecord.allocation_id == allocation_id,
                BudgetLedgerEntryRecord.operation_key == operation_key,
            )
        )
        return self._entry(record) if record else None

    def list_ledger_entries(self, allocation_id: UUID) -> list[BudgetLedgerEntry]:
        records = self._session.scalars(
            select(BudgetLedgerEntryRecord)
            .where(BudgetLedgerEntryRecord.allocation_id == allocation_id)
            .order_by(BudgetLedgerEntryRecord.created_at)
        )
        return [self._entry(record) for record in records]

    def add_economic_evidence(self, value: EconomicEvidence) -> None:
        data = dict(value.__dict__)
        data["kind"] = value.kind.value
        data["verification"] = value.verification.value
        self._session.add(EconomicEvidenceRecord(**data))

    def get_economic_evidence_by_external_ref(
        self, company_id: UUID, external_ref: str
    ) -> EconomicEvidence | None:
        record = self._session.scalar(
            select(EconomicEvidenceRecord).where(
                EconomicEvidenceRecord.company_id == company_id,
                EconomicEvidenceRecord.external_ref == external_ref,
            )
        )
        return self._evidence(record) if record else None

    def list_economic_evidence(self, company_id: UUID) -> list[EconomicEvidence]:
        records = self._session.scalars(
            select(EconomicEvidenceRecord)
            .where(EconomicEvidenceRecord.company_id == company_id)
            .order_by(EconomicEvidenceRecord.occurred_at)
        )
        return [self._evidence(record) for record in records]

    def add_expense_request(self, value: ExpenseRequest) -> None:
        data = dict(value.__dict__)
        data["status"] = value.status.value
        self._session.add(ExpenseRequestRecord(**data))

    def get_expense_request(
        self, request_id: UUID, *, for_update: bool = False
    ) -> ExpenseRequest | None:
        record = self._session.get(
            ExpenseRequestRecord, request_id, with_for_update=for_update
        )
        return self._expense(record) if record else None

    def save_expense_request(self, value: ExpenseRequest) -> None:
        record = self._required(ExpenseRequestRecord, value.id)
        record.status = value.status.value
        record.reviewed_by = value.reviewed_by
        record.review_reason = value.review_reason
        record.reviewed_at = value.reviewed_at

    def list_expense_requests(self, company_id: UUID) -> list[ExpenseRequest]:
        records = self._session.scalars(
            select(ExpenseRequestRecord)
            .where(ExpenseRequestRecord.company_id == company_id)
            .order_by(ExpenseRequestRecord.created_at)
        )
        return [self._expense(record) for record in records]

    @staticmethod
    def _allocation_data(value: BudgetAllocation) -> dict:
        data = dict(value.__dict__)
        data["scope_type"] = value.scope_type.value
        data["status"] = value.status.value
        return data

    @classmethod
    def _allocation(cls, record: BudgetAllocationRecord) -> BudgetAllocation:
        data = cls._record_data(record)
        data["scope_type"] = AllocationScope(data["scope_type"])
        data["status"] = AllocationStatus(data["status"])
        return BudgetAllocation(**data)

    @classmethod
    def _entry(cls, record: BudgetLedgerEntryRecord) -> BudgetLedgerEntry:
        data = cls._record_data(record)
        data["entry_type"] = BudgetEntryType(data["entry_type"])
        return BudgetLedgerEntry(**data)

    @classmethod
    def _evidence(cls, record: EconomicEvidenceRecord) -> EconomicEvidence:
        data = cls._record_data(record)
        data["kind"] = EconomicEvidenceKind(data["kind"])
        data["verification"] = EvidenceVerification(data["verification"])
        return EconomicEvidence(**data)

    @classmethod
    def _expense(cls, record: ExpenseRequestRecord) -> ExpenseRequest:
        data = cls._record_data(record)
        data["status"] = ExpenseStatus(data["status"])
        return ExpenseRequest(**data)

    def _required(self, model, value_id: UUID):
        record = self._session.get(model, value_id)
        if record is None:
            raise LookupError(f"{model.__name__} {value_id} was not found")
        return record

    @staticmethod
    def _record_data(record) -> dict:
        return {
            column.name: getattr(record, column.name)
            for column in record.__table__.columns
        }
