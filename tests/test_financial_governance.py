from datetime import timezone

import pytest
from fastapi.testclient import TestClient

from agentmesh.api.app import create_app
from agentmesh.application.company_services import CompanyModelService
from agentmesh.application.financial_governance_services import (
    FinancialGovernanceService,
)
from agentmesh.domain.errors import (
    FinancialGovernanceConflict,
    InvalidFinancialRecord,
)
from agentmesh.domain.financial_governance import (
    AllocationScope,
    EconomicEvidenceKind,
    EvidenceVerification,
    ExpenseStatus,
)
from agentmesh.domain.tasks import utc_now
from agentmesh.features import FeatureGateSet


def _company(company_service: CompanyModelService):
    return company_service.create_company(
        name="Evidence Company",
        mission="Operate within approved capital and report only evidenced economics.",
        owner_principal_id="owner",
        default_currency="USD",
    )


def _root(service: FinancialGovernanceService, company_id):
    return service.create_allocation(
        company_id,
        scope_type=AllocationScope.COMPANY,
        scope_id=str(company_id),
        currency="USD",
        approved_limit_micros=10_000_000,
        policy_version=1,
    )


def test_hierarchical_budget_reservation_settlement_and_idempotency(
    financial_governance_service,
    company_service,
):
    company = _company(company_service)
    root = _root(financial_governance_service, company.id)
    child = financial_governance_service.create_allocation(
        company.id,
        parent_allocation_id=root.allocation.id,
        scope_type=AllocationScope.ORGANIZATION_UNIT,
        scope_id="research",
        currency="USD",
        approved_limit_micros=4_000_000,
        policy_version=1,
    )

    reservation = financial_governance_service.reserve(
        company.id,
        child.allocation.id,
        amount_micros=3_000_000,
        operation_key="task:one:reserve",
        actor="controller",
    )
    repeated = financial_governance_service.reserve(
        company.id,
        child.allocation.id,
        amount_micros=3_000_000,
        operation_key="task:one:reserve",
        actor="controller",
    )
    assert repeated.id == reservation.id

    balances = {
        value.allocation.id: value
        for value in financial_governance_service.list_allocations(company.id)
    }
    assert balances[root.allocation.id].reserved_micros == 3_000_000
    assert balances[child.allocation.id].available_micros == 1_000_000

    financial_governance_service.settle(
        company.id,
        child.allocation.id,
        amount_micros=2_000_000,
        operation_key="task:one:settle",
        actor="controller",
        evidence_ref="usage:one",
    )
    child_balance = {
        value.allocation.id: value
        for value in financial_governance_service.list_allocations(company.id)
    }[child.allocation.id]
    assert child_balance.reserved_micros == 1_000_000
    assert child_balance.settled_micros == 2_000_000
    assert child_balance.available_micros == 1_000_000

    with pytest.raises(FinancialGovernanceConflict):
        financial_governance_service.reserve(
            company.id,
            child.allocation.id,
            amount_micros=1_000_001,
            operation_key="task:two:reserve",
            actor="controller",
        )


def test_expense_review_enforces_separation_and_reserves_budget(
    financial_governance_service,
    company_service,
):
    company = _company(company_service)
    allocation = _root(financial_governance_service, company.id)
    expense = financial_governance_service.propose_expense(
        company.id,
        allocation_id=allocation.allocation.id,
        purpose="Acquire a bounded research dataset.",
        vendor_ref="vendor:catalog",
        amount_micros=750_000,
        currency="USD",
        risk_tier="R3_COMMITMENT",
        evidence_refs=["quote:123"],
        requested_by="research-agent",
    )
    with pytest.raises(InvalidFinancialRecord, match="own request"):
        financial_governance_service.review_expense(
            company.id,
            expense.id,
            approved=True,
            reviewer="research-agent",
            reason="Self approval",
        )

    reviewed = financial_governance_service.review_expense(
        company.id,
        expense.id,
        approved=True,
        reviewer="finance-controller",
        reason="Within approved research allocation.",
    )
    assert reviewed.status is ExpenseStatus.APPROVED
    assert financial_governance_service.list_allocations(company.id)[
        0
    ].reserved_micros == 750_000


def test_dashboard_never_promotes_estimates_to_verified_revenue(
    financial_governance_service,
    company_service,
):
    company = _company(company_service)
    _root(financial_governance_service, company.id)
    financial_governance_service.record_evidence(
        company.id,
        kind=EconomicEvidenceKind.OPPORTUNITY,
        verification=EvidenceVerification.ESTIMATED,
        amount_micros=9_000_000,
        currency="USD",
        attribution_method="DIRECT",
        recorded_by="sales-agent",
        occurred_at=utc_now().astimezone(timezone.utc),
    )
    financial_governance_service.record_evidence(
        company.id,
        kind=EconomicEvidenceKind.COLLECTED_CASH,
        verification=EvidenceVerification.VERIFIED,
        amount_micros=3_000_000,
        currency="USD",
        external_ref="payment:123",
        source_snapshot_digest="a" * 64,
        attribution_method="DIRECT",
        recorded_by="accounting-adapter",
        occurred_at=utc_now().astimezone(timezone.utc),
    )
    financial_governance_service.record_evidence(
        company.id,
        kind=EconomicEvidenceKind.SETTLED_COST,
        verification=EvidenceVerification.VERIFIED,
        amount_micros=1_250_000,
        currency="USD",
        external_ref="expense:123",
        source_snapshot_digest="b" * 64,
        attribution_method="DIRECT",
        recorded_by="accounting-adapter",
        occurred_at=utc_now().astimezone(timezone.utc),
    )
    dashboard = financial_governance_service.dashboard(company.id)
    assert dashboard.estimated_pipeline_micros == 9_000_000
    assert dashboard.collected_cash_micros == 3_000_000
    assert dashboard.verified_margin_micros == 1_750_000

    with pytest.raises(InvalidFinancialRecord, match="VERIFIED"):
        financial_governance_service.record_evidence(
            company.id,
            kind=EconomicEvidenceKind.INVOICED_REVENUE,
            verification=EvidenceVerification.ESTIMATED,
            amount_micros=1,
            currency="USD",
            attribution_method="DIRECT",
            recorded_by="agent",
            occurred_at=utc_now(),
        )


def test_finance_api_exposes_allocations_entries_and_dashboard(
    application_container,
    company_service,
):
    company = _company(company_service)
    application_container.feature_gates = FeatureGateSet.from_config(
        "full",
        "company_model=true,company_finance_read=true,financial_governance=true",
    )
    with TestClient(create_app(application_container)) as client:
        created = client.post(
            f"/api/v1/companies/{company.id}/finance/allocations",
            json={
                "scope_type": "COMPANY",
                "scope_id": str(company.id),
                "currency": "USD",
                "approved_limit_micros": 2_000_000,
                "policy_version": 1,
            },
        )
        assert created.status_code == 201
        allocation_id = created.json()["id"]
        reserved = client.post(
            f"/api/v1/companies/{company.id}/finance/allocations/"
            f"{allocation_id}/reserve",
            json={
                "amount_micros": 500_000,
                "operation_key": "api:reserve",
            },
        )
        assert reserved.status_code == 200
        assert reserved.json()["entry_type"] == "RESERVE"
        dashboard = client.get(
            f"/api/v1/companies/{company.id}/finance/dashboard"
        )
        assert dashboard.status_code == 200
        assert dashboard.json()["active_reserved_micros"] == 500_000
