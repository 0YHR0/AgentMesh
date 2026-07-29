from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.domain.company import CompanyStatus
from agentmesh.domain.errors import (
    FinancialGovernanceConflict,
    FinancialRecordNotFound,
    InvalidFinancialRecord,
)
from agentmesh.domain.financial_governance import (
    AllocationScope,
    AllocationStatus,
    BudgetAllocation,
    BudgetEntryType,
    BudgetLedgerEntry,
    EconomicEvidence,
    EconomicEvidenceKind,
    ExpenseRequest,
)
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.features import Feature, FeatureGateSet


@dataclass(frozen=True)
class AllocationBalance:
    allocation: BudgetAllocation
    reserved_micros: int
    settled_micros: int
    available_micros: int


@dataclass(frozen=True)
class FinanceDashboard:
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


class FinancialGovernanceService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        tenant_id: str,
        feature_gates: FeatureGateSet,
    ) -> None:
        self._uow_factory = uow_factory
        self._tenant_id = tenant_id
        self._feature_gates = feature_gates

    def create_allocation(
        self,
        company_id: UUID,
        *,
        scope_type: AllocationScope,
        scope_id: str,
        currency: str,
        approved_limit_micros: int,
        policy_version: int,
        parent_allocation_id: UUID | None = None,
    ) -> AllocationBalance:
        self._require_governance()
        value = BudgetAllocation.create(
            company_id=company_id,
            scope_type=scope_type,
            scope_id=scope_id,
            currency=currency,
            approved_limit_micros=approved_limit_micros,
            policy_version=policy_version,
            parent_allocation_id=parent_allocation_id,
        )
        with self._uow_factory() as uow:
            self._active_company(uow, company_id)
            if parent_allocation_id:
                parent = self._allocation(
                    uow, company_id, parent_allocation_id, for_update=True
                )
                if parent.status is not AllocationStatus.ACTIVE:
                    raise FinancialGovernanceConflict(
                        "Parent Budget Allocation is closed"
                    )
                if parent.currency != value.currency:
                    raise InvalidFinancialRecord(
                        "Child and parent allocations must use the same currency"
                    )
            elif scope_type is not AllocationScope.COMPANY:
                raise InvalidFinancialRecord(
                    "A root Budget Allocation must use COMPANY scope"
                )
            uow.financial_governance.add_allocation(value)
            self._emit(
                uow,
                "budget-allocation.created",
                company_id,
                value.id,
                {
                    "scope_type": value.scope_type.value,
                    "scope_id": value.scope_id,
                    "approved_limit_micros": value.approved_limit_micros,
                    "currency": value.currency,
                },
            )
            uow.commit()
            return AllocationBalance(value, 0, 0, value.approved_limit_micros)

    def list_allocations(self, company_id: UUID) -> list[AllocationBalance]:
        self._require_read()
        with self._uow_factory() as uow:
            self._company(uow, company_id)
            return [
                self._balance(uow, value)
                for value in uow.financial_governance.list_allocations(company_id)
            ]

    def close_allocation(
        self, company_id: UUID, allocation_id: UUID
    ) -> AllocationBalance:
        self._require_governance()
        with self._uow_factory() as uow:
            self._active_company(uow, company_id)
            value = self._allocation(uow, company_id, allocation_id, for_update=True)
            balance = self._balance(uow, value)
            if balance.reserved_micros:
                raise FinancialGovernanceConflict(
                    "Budget Allocation with active reservations cannot be closed"
                )
            value.close()
            uow.financial_governance.save_allocation(value)
            self._emit(uow, "budget-allocation.closed", company_id, value.id, {})
            uow.commit()
            return self._balance(uow, value)

    def reserve(
        self,
        company_id: UUID,
        allocation_id: UUID,
        *,
        amount_micros: int,
        operation_key: str,
        actor: str,
        task_id: UUID | None = None,
        evidence_ref: str | None = None,
    ) -> BudgetLedgerEntry:
        return self._budget_entry(
            company_id,
            allocation_id,
            entry_type=BudgetEntryType.RESERVE,
            amount_micros=amount_micros,
            operation_key=operation_key,
            actor=actor,
            task_id=task_id,
            evidence_ref=evidence_ref,
        )

    def release(
        self,
        company_id: UUID,
        allocation_id: UUID,
        *,
        amount_micros: int,
        operation_key: str,
        actor: str,
        task_id: UUID | None = None,
        evidence_ref: str | None = None,
    ) -> BudgetLedgerEntry:
        return self._budget_entry(
            company_id,
            allocation_id,
            entry_type=BudgetEntryType.RELEASE,
            amount_micros=amount_micros,
            operation_key=operation_key,
            actor=actor,
            task_id=task_id,
            evidence_ref=evidence_ref,
        )

    def settle(
        self,
        company_id: UUID,
        allocation_id: UUID,
        *,
        amount_micros: int,
        operation_key: str,
        actor: str,
        task_id: UUID | None = None,
        evidence_ref: str | None = None,
    ) -> BudgetLedgerEntry:
        return self._budget_entry(
            company_id,
            allocation_id,
            entry_type=BudgetEntryType.SETTLE,
            amount_micros=amount_micros,
            operation_key=operation_key,
            actor=actor,
            task_id=task_id,
            evidence_ref=evidence_ref,
        )

    def record_evidence(
        self, company_id: UUID, **values: Any
    ) -> EconomicEvidence:
        self._require_governance()
        value = EconomicEvidence.create(company_id=company_id, **values)
        with self._uow_factory() as uow:
            company = self._active_company(uow, company_id)
            if value.currency != company.default_currency:
                raise InvalidFinancialRecord(
                    "Economic evidence must use the Company default currency"
                )
            duplicate = (
                value.external_ref
                and uow.financial_governance.get_economic_evidence_by_external_ref(
                    company_id, value.external_ref
                )
            )
            if duplicate:
                raise FinancialGovernanceConflict(
                    "Economic evidence external reference already exists"
                )
            uow.financial_governance.add_economic_evidence(value)
            self._emit(
                uow,
                "economic-evidence.recorded",
                company_id,
                value.id,
                {
                    "kind": value.kind.value,
                    "verification": value.verification.value,
                    "amount_micros": value.amount_micros,
                    "currency": value.currency,
                    "evidence_digest": value.evidence_digest,
                },
            )
            uow.commit()
            return value

    def list_evidence(self, company_id: UUID) -> list[EconomicEvidence]:
        self._require_read()
        with self._uow_factory() as uow:
            self._company(uow, company_id)
            return uow.financial_governance.list_economic_evidence(company_id)

    def propose_expense(
        self, company_id: UUID, *, requested_by: str, **values: Any
    ) -> ExpenseRequest:
        self._require_governance()
        value = ExpenseRequest.propose(
            company_id=company_id, requested_by=requested_by, **values
        )
        with self._uow_factory() as uow:
            self._active_company(uow, company_id)
            allocation = self._allocation(uow, company_id, value.allocation_id)
            if allocation.status is not AllocationStatus.ACTIVE:
                raise FinancialGovernanceConflict("Budget Allocation is closed")
            if allocation.currency != value.currency:
                raise InvalidFinancialRecord(
                    "Expense and Budget Allocation currencies must match"
                )
            uow.financial_governance.add_expense_request(value)
            self._emit(
                uow,
                "expense.proposed",
                company_id,
                value.id,
                {
                    "allocation_id": str(value.allocation_id),
                    "amount_micros": value.amount_micros,
                    "currency": value.currency,
                    "risk_tier": value.risk_tier,
                },
            )
            uow.commit()
            return value

    def review_expense(
        self,
        company_id: UUID,
        request_id: UUID,
        *,
        approved: bool,
        reviewer: str,
        reason: str,
    ) -> ExpenseRequest:
        self._require_governance()
        with self._uow_factory() as uow:
            self._active_company(uow, company_id)
            value = uow.financial_governance.get_expense_request(
                request_id, for_update=True
            )
            if value is None or value.company_id != company_id:
                raise FinancialRecordNotFound(
                    f"Expense Request {request_id} was not found"
                )
            value.review(approved=approved, reviewer=reviewer, reason=reason)
            if approved:
                allocation = self._allocation(
                    uow, company_id, value.allocation_id, for_update=True
                )
                lineage = self._ensure_capacity(
                    uow, allocation, value.amount_micros
                )
                for item in lineage:
                    uow.financial_governance.add_ledger_entry(
                        BudgetLedgerEntry.create(
                        allocation_id=item.id,
                        entry_type=BudgetEntryType.RESERVE,
                        amount_micros=value.amount_micros,
                        operation_key=f"expense:{value.id}:approval",
                        actor=reviewer,
                        evidence_ref=f"expense:{value.id}",
                        )
                    )
            uow.financial_governance.save_expense_request(value)
            self._emit(
                uow,
                "expense.reviewed",
                company_id,
                value.id,
                {"status": value.status.value, "reviewer": reviewer},
            )
            uow.commit()
            return value

    def list_expenses(self, company_id: UUID) -> list[ExpenseRequest]:
        self._require_read()
        with self._uow_factory() as uow:
            self._company(uow, company_id)
            return uow.financial_governance.list_expense_requests(company_id)

    def dashboard(self, company_id: UUID) -> FinanceDashboard:
        self._require_read()
        with self._uow_factory() as uow:
            company = self._company(uow, company_id)
            evidence = uow.financial_governance.list_economic_evidence(company_id)
            totals = {
                kind: sum(item.amount_micros for item in evidence if item.kind is kind)
                for kind in EconomicEvidenceKind
            }
            allocations = uow.financial_governance.list_allocations(company_id)
            reserved = sum(
                self._balance(uow, item).reserved_micros
                for item in allocations
                if item.parent_allocation_id is None
            )
            expenses = uow.financial_governance.list_expense_requests(company_id)
            return FinanceDashboard(
                currency=company.default_currency,
                estimated_pipeline_micros=totals[EconomicEvidenceKind.OPPORTUNITY],
                estimated_offers_micros=totals[EconomicEvidenceKind.OFFER],
                contracted_revenue_micros=totals[
                    EconomicEvidenceKind.CONTRACTED_REVENUE
                ],
                invoiced_revenue_micros=totals[
                    EconomicEvidenceKind.INVOICED_REVENUE
                ],
                collected_cash_micros=totals[EconomicEvidenceKind.COLLECTED_CASH],
                settled_cost_micros=totals[EconomicEvidenceKind.SETTLED_COST],
                verified_margin_micros=totals[
                    EconomicEvidenceKind.COLLECTED_CASH
                ]
                - totals[EconomicEvidenceKind.SETTLED_COST],
                active_reserved_micros=reserved,
                open_expense_requests=sum(
                    item.status.value == "PROPOSED" for item in expenses
                ),
            )

    def _budget_entry(
        self,
        company_id: UUID,
        allocation_id: UUID,
        *,
        entry_type: BudgetEntryType,
        amount_micros: int,
        operation_key: str,
        actor: str,
        task_id: UUID | None,
        evidence_ref: str | None,
    ) -> BudgetLedgerEntry:
        self._require_governance()
        candidate = BudgetLedgerEntry.create(
            allocation_id=allocation_id,
            entry_type=entry_type,
            amount_micros=amount_micros,
            operation_key=operation_key,
            actor=actor,
            task_id=task_id,
            evidence_ref=evidence_ref,
        )
        with self._uow_factory() as uow:
            self._active_company(uow, company_id)
            existing = uow.financial_governance.get_ledger_entry_by_key(
                allocation_id, candidate.operation_key
            )
            if existing:
                if (
                    existing.entry_type != candidate.entry_type
                    or existing.amount_micros != candidate.amount_micros
                ):
                    raise FinancialGovernanceConflict(
                        "Budget operation key was reused with different semantics"
                    )
                return existing
            allocation = self._allocation(
                uow, company_id, allocation_id, for_update=True
            )
            if allocation.status is not AllocationStatus.ACTIVE:
                raise FinancialGovernanceConflict("Budget Allocation is closed")
            balance = self._balance(uow, allocation)
            if entry_type is BudgetEntryType.RESERVE:
                lineage = self._ensure_capacity(uow, allocation, amount_micros)
            elif amount_micros > balance.reserved_micros:
                raise FinancialGovernanceConflict(
                    "Release or settlement exceeds the active reservation"
                )
            else:
                lineage = self._locked_lineage(uow, allocation)
                if any(
                    amount_micros > self._balance(uow, item).reserved_micros
                    for item in lineage
                ):
                    raise FinancialGovernanceConflict(
                        "Ancestor release or settlement exceeds its reservation"
                    )
            if any(
                uow.financial_governance.get_ledger_entry_by_key(
                    item.id, operation_key
                )
                for item in lineage
                if item.id != allocation.id
            ):
                raise FinancialGovernanceConflict(
                    "Budget operation key is already used in the allocation hierarchy"
                )
            for item in lineage:
                uow.financial_governance.add_ledger_entry(
                    candidate
                    if item.id == allocation.id
                    else BudgetLedgerEntry.create(
                        allocation_id=item.id,
                        entry_type=entry_type,
                        amount_micros=amount_micros,
                        operation_key=operation_key,
                        actor=actor,
                        task_id=task_id,
                        evidence_ref=evidence_ref,
                    )
                )
            self._emit(
                uow,
                f"budget.{entry_type.value.lower()}d",
                company_id,
                candidate.id,
                {
                    "allocation_id": str(allocation_id),
                    "amount_micros": amount_micros,
                    "currency": allocation.currency,
                    "operation_key": candidate.operation_key,
                },
            )
            uow.commit()
            return candidate

    def _ensure_capacity(
        self, uow: Any, allocation: BudgetAllocation, amount_micros: int
    ) -> list[BudgetAllocation]:
        lineage = self._locked_lineage(uow, allocation)
        for current in lineage:
            if current.status is not AllocationStatus.ACTIVE:
                raise FinancialGovernanceConflict(
                    f"Budget Allocation {current.id} is closed"
                )
            balance = self._balance(uow, current)
            if amount_micros > balance.available_micros:
                raise FinancialGovernanceConflict(
                    f"Budget Allocation {current.id} has insufficient availability"
                )
        return lineage

    def _locked_lineage(
        self, uow: Any, allocation: BudgetAllocation
    ) -> list[BudgetAllocation]:
        current = allocation
        lineage: list[BudgetAllocation] = []
        seen: set[UUID] = set()
        while True:
            if current.id in seen:
                raise FinancialGovernanceConflict("Budget hierarchy contains a cycle")
            seen.add(current.id)
            lineage.append(current)
            if current.parent_allocation_id is None:
                return lineage
            current = self._allocation(
                uow,
                allocation.company_id,
                current.parent_allocation_id,
                for_update=True,
            )

    @staticmethod
    def _balance(uow: Any, value: BudgetAllocation) -> AllocationBalance:
        entries = uow.financial_governance.list_ledger_entries(value.id)
        reserved = sum(
            item.amount_micros
            if item.entry_type is BudgetEntryType.RESERVE
            else -item.amount_micros
            for item in entries
        )
        settled = sum(
            item.amount_micros
            for item in entries
            if item.entry_type is BudgetEntryType.SETTLE
        )
        return AllocationBalance(
            allocation=value,
            reserved_micros=reserved,
            settled_micros=settled,
            available_micros=value.approved_limit_micros - reserved - settled,
        )

    def _company(self, uow: Any, company_id: UUID):
        company = uow.company_model.get_company(company_id)
        if company is None or company.tenant_id != self._tenant_id:
            raise FinancialRecordNotFound(f"Company {company_id} was not found")
        return company

    def _active_company(self, uow: Any, company_id: UUID):
        company = self._company(uow, company_id)
        if company.status is not CompanyStatus.ACTIVE:
            raise FinancialGovernanceConflict(
                "Archived Company cannot manage financial records"
            )
        return company

    @staticmethod
    def _allocation(
        uow: Any,
        company_id: UUID,
        allocation_id: UUID,
        *,
        for_update: bool = False,
    ) -> BudgetAllocation:
        value = uow.financial_governance.get_allocation(
            allocation_id, for_update=for_update
        )
        if value is None or value.company_id != company_id:
            raise FinancialRecordNotFound(
                f"Budget Allocation {allocation_id} was not found"
            )
        return value

    def _require_read(self) -> None:
        self._feature_gates.require(Feature.COMPANY_FINANCE_READ)

    def _require_governance(self) -> None:
        self._feature_gates.require(Feature.FINANCIAL_GOVERNANCE)

    def _emit(
        self,
        uow: Any,
        suffix: str,
        company_id: UUID,
        aggregate_id: UUID,
        payload: dict[str, Any],
    ) -> None:
        uow.outbox.add(
            MessageEnvelope.domain_event(
                schema_name=f"agentmesh.company.finance.{suffix}",
                tenant_id=self._tenant_id,
                aggregate_id=aggregate_id,
                payload={"company_id": str(company_id), **payload},
            )
        )
