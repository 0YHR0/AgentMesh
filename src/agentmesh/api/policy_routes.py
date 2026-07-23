from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, status
from pydantic import BaseModel, Field

from agentmesh.api.feature_routes import require_feature
from agentmesh.api.security import (
    PrincipalDependency,
    get_principal_context,
    require_permission,
)
from agentmesh.application.policy_services import GovernedActionResult, PolicyApprovalService
from agentmesh.domain.identity import Permission
from agentmesh.domain.policy import (
    ApprovalOutcome,
    ApprovalStatus,
    GovernedActionType,
    PolicyResult,
)
from agentmesh.features import Feature

router = APIRouter(
    prefix="/api/v1",
    tags=["policy-approval"],
    dependencies=[
        Depends(get_principal_context),
        Depends(require_feature(Feature.POLICY_APPROVAL)),
    ],
)


class RequestGovernedAction(BaseModel):
    action_type: GovernedActionType
    resource_type: str = Field(min_length=1, max_length=64)
    resource_id: UUID
    arguments: dict[str, Any] = Field(default_factory=dict)


class DecideApprovalRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2_000)


class ApprovalDecisionResponse(BaseModel):
    id: UUID
    approver_id: str
    outcome: ApprovalOutcome
    stage: str
    reason: str
    created_at: datetime


class GovernedActionResponse(BaseModel):
    id: UUID
    requester_id: str
    action_type: GovernedActionType
    resource_type: str
    resource_id: UUID
    arguments: dict[str, Any]
    action_hash: str
    canonicalization_version: str
    policy_result: PolicyResult
    reason_code: str
    policy_bundle: str
    policy_version: str
    obligations: dict[str, Any]
    approval_stages: list[dict[str, Any]]
    current_stage: int
    approval_id: UUID | None
    approval_status: ApprovalStatus
    permit_id: UUID | None
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None
    consumed_at: datetime | None
    decisions: list[ApprovalDecisionResponse]

    @classmethod
    def from_result(cls, result: GovernedActionResult) -> GovernedActionResponse:
        action = result.action
        return cls(
            id=action.id,
            requester_id=action.requester_id,
            action_type=action.action_type,
            resource_type=action.resource_type,
            resource_id=action.resource_id,
            arguments=action.arguments,
            action_hash=action.action_hash,
            canonicalization_version=action.canonicalization_version,
            policy_result=action.policy_result,
            reason_code=action.reason_code,
            policy_bundle=action.policy_bundle,
            policy_version=action.policy_version,
            obligations=action.obligations,
            approval_stages=[
                {
                    "name": stage.name,
                    "quorum": stage.quorum,
                    "eligible_roles": [role.value for role in stage.eligible_roles],
                }
                for stage in action.approval_stages
            ],
            current_stage=action.current_stage,
            approval_id=action.approval_id,
            approval_status=action.approval_status,
            permit_id=action.permit_id,
            created_at=action.created_at,
            expires_at=action.expires_at,
            decided_at=action.decided_at,
            consumed_at=action.consumed_at,
            decisions=[
                ApprovalDecisionResponse(
                    id=value.id,
                    approver_id=value.approver_id,
                    outcome=value.outcome,
                    stage=value.stage,
                    reason=value.reason,
                    created_at=value.created_at,
                )
                for value in result.decisions
            ],
        )


class GovernedActionListResponse(BaseModel):
    items: list[GovernedActionResponse]
    limit: int
    offset: int


def get_policy_service(request: Request) -> PolicyApprovalService:
    return request.app.state.container.policy_service


PolicyServiceDependency = Annotated[PolicyApprovalService, Depends(get_policy_service)]
LimitQuery = Annotated[int, Query(ge=1, le=100)]
OffsetQuery = Annotated[int, Query(ge=0)]
ApprovalStatusQuery = Annotated[ApprovalStatus | None, Query(alias="status")]
IdempotencyHeader = Annotated[str | None, Header(alias="Idempotency-Key", max_length=255)]


@router.post(
    "/policy/actions",
    response_model=GovernedActionResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(Permission.POLICY_REQUEST))],
)
def request_governed_action(
    payload: RequestGovernedAction,
    principal: PrincipalDependency,
    service: PolicyServiceDependency,
    idempotency_key: IdempotencyHeader = None,
) -> GovernedActionResponse:
    return GovernedActionResponse.from_result(
        service.request_action(
            principal=principal,
            action_type=payload.action_type,
            resource_type=payload.resource_type,
            resource_id=payload.resource_id,
            arguments=payload.arguments,
            idempotency_key=idempotency_key,
        )
    )


@router.get(
    "/approvals",
    response_model=GovernedActionListResponse,
    dependencies=[Depends(require_permission(Permission.APPROVAL_READ))],
)
def list_approvals(
    service: PolicyServiceDependency,
    approval_status: ApprovalStatusQuery = None,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
) -> GovernedActionListResponse:
    values = service.list_approvals(status=approval_status, limit=limit, offset=offset)
    return GovernedActionListResponse(
        items=[GovernedActionResponse.from_result(value) for value in values],
        limit=limit,
        offset=offset,
    )


def _decide(
    approval_id: UUID,
    payload: DecideApprovalRequest,
    principal: PrincipalDependency,
    service: PolicyServiceDependency,
    outcome: ApprovalOutcome,
) -> GovernedActionResponse:
    return GovernedActionResponse.from_result(
        service.decide(
            approval_id,
            principal=principal,
            outcome=outcome,
            reason=payload.reason,
        )
    )


@router.post(
    "/approvals/{approval_id}/approve",
    response_model=GovernedActionResponse,
    dependencies=[Depends(require_permission(Permission.APPROVAL_DECIDE))],
)
def approve(
    approval_id: UUID,
    payload: DecideApprovalRequest,
    principal: PrincipalDependency,
    service: PolicyServiceDependency,
) -> GovernedActionResponse:
    return _decide(approval_id, payload, principal, service, ApprovalOutcome.APPROVE)


@router.post(
    "/approvals/{approval_id}/reject",
    response_model=GovernedActionResponse,
    dependencies=[Depends(require_permission(Permission.APPROVAL_DECIDE))],
)
def reject(
    approval_id: UUID,
    payload: DecideApprovalRequest,
    principal: PrincipalDependency,
    service: PolicyServiceDependency,
) -> GovernedActionResponse:
    return _decide(approval_id, payload, principal, service, ApprovalOutcome.REJECT)
