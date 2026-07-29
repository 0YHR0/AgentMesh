import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.application.company_services import CompanyModelService
from agentmesh.application.financial_governance_services import (
    FinancialGovernanceService,
)
from agentmesh.config import get_settings
from agentmesh.domain.financial_governance import AllocationScope
from agentmesh.features import FeatureGateSet
from agentmesh.infrastructure.postgres.uow import SqlAlchemyUnitOfWorkFactory

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run service integration tests",
    ),
]


def test_hierarchical_budget_ledger_round_trip_in_postgres() -> None:
    settings = get_settings()
    tenant_id = f"financial-governance-{uuid4().hex}"
    engine = create_engine(settings.database_url)
    factory = SqlAlchemyUnitOfWorkFactory(
        sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    )
    gates = FeatureGateSet.from_config(
        "full",
        "company_model=true,company_finance_read=true,financial_governance=true",
    )
    company_service = CompanyModelService(
        uow_factory=factory, tenant_id=tenant_id, feature_gates=gates
    )
    service = FinancialGovernanceService(
        uow_factory=factory, tenant_id=tenant_id, feature_gates=gates
    )
    try:
        company = company_service.create_company(
            name="Finance Integration Company",
            mission="Persist evidence-classified economics and bounded capital.",
            owner_principal_id="integration-owner",
        )
        root = service.create_allocation(
            company.id,
            scope_type=AllocationScope.COMPANY,
            scope_id=str(company.id),
            currency="USD",
            approved_limit_micros=5_000_000,
            policy_version=1,
        )
        child = service.create_allocation(
            company.id,
            parent_allocation_id=root.allocation.id,
            scope_type=AllocationScope.INITIATIVE,
            scope_id="market-study",
            currency="USD",
            approved_limit_micros=2_000_000,
            policy_version=1,
        )
        service.reserve(
            company.id,
            child.allocation.id,
            amount_micros=1_000_000,
            operation_key="integration:reserve",
            actor="integration-controller",
        )
        balances = {
            value.allocation.id: value for value in service.list_allocations(company.id)
        }
        assert balances[root.allocation.id].reserved_micros == 1_000_000
        assert balances[child.allocation.id].reserved_micros == 1_000_000

        with engine.connect() as connection:
            mirrored = connection.execute(
                text(
                    "SELECT count(*) FROM budget_ledger_entries "
                    "WHERE operation_key = :operation_key"
                ),
                {"operation_key": "integration:reserve"},
            ).scalar_one()
        assert mirrored == 2
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM companies WHERE tenant_id = :tenant_id"),
                {"tenant_id": tenant_id},
            )
        engine.dispose()
