from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, model_validator
from typing_extensions import Self

from agentmesh.api.feature_routes import FeatureGatesDependency
from agentmesh.api.security import PrincipalDependency, require_read_or_write_permission
from agentmesh.application.quota_services import QuotaPolicyService
from agentmesh.domain.identity import Permission
from agentmesh.domain.quotas import QuotaPolicyStatus, QuotaScope
from agentmesh.features import Feature

router = APIRouter(
    prefix="/api/v1/quotas",
    tags=["quotas"],
    dependencies=[
        Depends(require_read_or_write_permission(Permission.QUOTA_READ, Permission.QUOTA_MANAGE))
    ],
)


def get_service(request: Request) -> QuotaPolicyService:
    return request.app.state.container.quota_policy_service


ServiceDependency = Annotated[QuotaPolicyService, Depends(get_service)]


class PutQuotaPolicyRequest(BaseModel):
    scope: QuotaScope
    project_id: str | None = Field(default=None, min_length=1, max_length=128)
    max_concurrent_attempts: int = Field(ge=1, le=100_000)
    weight: int = Field(default=1, ge=1, le=1_000)

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if self.scope is QuotaScope.TENANT and self.project_id is not None:
            raise ValueError("Tenant quota must not specify project_id")
        if self.scope is QuotaScope.PROJECT and self.project_id is None:
            raise ValueError("Project quota requires project_id")
        return self


class QuotaPolicyResponse(BaseModel):
    id: UUID
    tenant_id: str
    scope: QuotaScope
    project_id: str | None
    max_concurrent_attempts: int
    weight: int
    version: int
    active_reservations: int
    created_by: str
    created_at: datetime

    @classmethod
    def from_status(cls, status: QuotaPolicyStatus) -> QuotaPolicyResponse:
        policy = status.policy
        return cls(
            id=policy.id,
            tenant_id=policy.tenant_id,
            scope=policy.scope,
            project_id=policy.project_id,
            max_concurrent_attempts=policy.max_concurrent_attempts,
            weight=policy.weight,
            version=policy.version,
            active_reservations=status.active_reservations,
            created_by=policy.created_by,
            created_at=policy.created_at,
        )


@router.put("/policies", response_model=QuotaPolicyResponse)
def put_policy(
    payload: PutQuotaPolicyRequest,
    principal: PrincipalDependency,
    service: ServiceDependency,
    feature_gates: FeatureGatesDependency,
) -> QuotaPolicyResponse:
    feature_gates.require(Feature.QUOTA_ADMISSION)
    return QuotaPolicyResponse.from_status(
        service.put_policy(
            scope=payload.scope,
            project_id=payload.project_id,
            max_concurrent_attempts=payload.max_concurrent_attempts,
            weight=payload.weight,
            created_by=principal.principal_id,
        )
    )


@router.get("/policies", response_model=list[QuotaPolicyResponse])
def list_policies(
    service: ServiceDependency, feature_gates: FeatureGatesDependency
) -> list[QuotaPolicyResponse]:
    feature_gates.require(Feature.QUOTA_ADMISSION)
    return [QuotaPolicyResponse.from_status(value) for value in service.list_status()]
