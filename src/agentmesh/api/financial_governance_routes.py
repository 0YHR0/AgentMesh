from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status

from agentmesh.api.feature_routes import require_feature
from agentmesh.api.financial_governance_schemas import (
    AllocationResponse,
    BudgetEntryRequest,
    BudgetEntryResponse,
    CreateAllocationRequest,
    EconomicEvidenceResponse,
    ExpenseResponse,
    FinanceDashboardResponse,
    ProposeExpenseRequest,
    RecordEconomicEvidenceRequest,
    ReviewExpenseRequest,
)
from agentmesh.api.security import PrincipalDependency, require_read_or_write_permission
from agentmesh.application.financial_governance_services import (
    FinancialGovernanceService,
)
from agentmesh.domain.identity import Permission
from agentmesh.features import Feature

router = APIRouter(
    prefix="/api/v1/companies/{company_id}/finance",
    tags=["financial-governance"],
    dependencies=[
        Depends(
            require_read_or_write_permission(
                Permission.COMPANY_READ, Permission.COMPANY_MANAGE
            )
        ),
        Depends(require_feature(Feature.COMPANY_FINANCE_READ)),
    ],
)


def get_service(request: Request) -> FinancialGovernanceService:
    return request.app.state.container.financial_governance_service


ServiceDependency = Annotated[FinancialGovernanceService, Depends(get_service)]


@router.post(
    "/allocations",
    response_model=AllocationResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_feature(Feature.FINANCIAL_GOVERNANCE))],
)
def create_allocation(
    company_id: UUID,
    payload: CreateAllocationRequest,
    service: ServiceDependency,
) -> AllocationResponse:
    return AllocationResponse.from_balance(
        service.create_allocation(company_id, **payload.model_dump())
    )


@router.get("/allocations", response_model=list[AllocationResponse])
def list_allocations(
    company_id: UUID, service: ServiceDependency
) -> list[AllocationResponse]:
    return [
        AllocationResponse.from_balance(value)
        for value in service.list_allocations(company_id)
    ]


@router.post(
    "/allocations/{allocation_id}/close",
    response_model=AllocationResponse,
    dependencies=[Depends(require_feature(Feature.FINANCIAL_GOVERNANCE))],
)
def close_allocation(
    company_id: UUID, allocation_id: UUID, service: ServiceDependency
) -> AllocationResponse:
    return AllocationResponse.from_balance(
        service.close_allocation(company_id, allocation_id)
    )


@router.post(
    "/allocations/{allocation_id}/{action}",
    response_model=BudgetEntryResponse,
    dependencies=[Depends(require_feature(Feature.FINANCIAL_GOVERNANCE))],
)
def post_budget_entry(
    company_id: UUID,
    allocation_id: UUID,
    action: Literal["reserve", "release", "settle"],
    payload: BudgetEntryRequest,
    service: ServiceDependency,
    principal: PrincipalDependency,
) -> BudgetEntryResponse:
    method = getattr(service, action)
    return BudgetEntryResponse.model_validate(
        method(
            company_id,
            allocation_id,
            **payload.model_dump(),
            actor=principal.principal_id,
        )
    )


@router.post(
    "/evidence",
    response_model=EconomicEvidenceResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_feature(Feature.FINANCIAL_GOVERNANCE))],
)
def record_evidence(
    company_id: UUID,
    payload: RecordEconomicEvidenceRequest,
    service: ServiceDependency,
    principal: PrincipalDependency,
) -> EconomicEvidenceResponse:
    return EconomicEvidenceResponse.from_domain(
        service.record_evidence(
            company_id,
            **payload.model_dump(),
            recorded_by=principal.principal_id,
        )
    )


@router.get("/evidence", response_model=list[EconomicEvidenceResponse])
def list_evidence(
    company_id: UUID, service: ServiceDependency
) -> list[EconomicEvidenceResponse]:
    return [
        EconomicEvidenceResponse.from_domain(value)
        for value in service.list_evidence(company_id)
    ]


@router.post(
    "/expenses",
    response_model=ExpenseResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_feature(Feature.FINANCIAL_GOVERNANCE))],
)
def propose_expense(
    company_id: UUID,
    payload: ProposeExpenseRequest,
    service: ServiceDependency,
    principal: PrincipalDependency,
) -> ExpenseResponse:
    return ExpenseResponse.model_validate(
        service.propose_expense(
            company_id,
            **payload.model_dump(),
            requested_by=principal.principal_id,
        )
    )


@router.post(
    "/expenses/{request_id}/review",
    response_model=ExpenseResponse,
    dependencies=[Depends(require_feature(Feature.FINANCIAL_GOVERNANCE))],
)
def review_expense(
    company_id: UUID,
    request_id: UUID,
    payload: ReviewExpenseRequest,
    service: ServiceDependency,
    principal: PrincipalDependency,
) -> ExpenseResponse:
    return ExpenseResponse.model_validate(
        service.review_expense(
            company_id,
            request_id,
            approved=payload.approved,
            reason=payload.reason,
            reviewer=principal.principal_id,
        )
    )


@router.get("/expenses", response_model=list[ExpenseResponse])
def list_expenses(
    company_id: UUID, service: ServiceDependency
) -> list[ExpenseResponse]:
    return [
        ExpenseResponse.model_validate(value)
        for value in service.list_expenses(company_id)
    ]


@router.get("/dashboard", response_model=FinanceDashboardResponse)
def dashboard(
    company_id: UUID, service: ServiceDependency
) -> FinanceDashboardResponse:
    return FinanceDashboardResponse.from_domain(service.dashboard(company_id))
