from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, status
from pydantic import BaseModel

from agentmesh.api.feature_routes import require_feature
from agentmesh.api.security import PrincipalDependency, require_permission
from agentmesh.application.identity_services import IdentityAdministrationService
from agentmesh.domain.identity import (
    ExternalIdentity,
    Permission,
    Principal,
    PrincipalStatus,
    PrincipalType,
    Role,
    RoleBinding,
    RoleBindingStatus,
)
from agentmesh.features import Feature

router = APIRouter(
    prefix="/api/v1",
    tags=["identity"],
    dependencies=[
        Depends(require_permission(Permission.SYSTEM_INSPECT)),
        Depends(require_feature(Feature.IDENTITY_RBAC)),
    ],
)

admin_router = APIRouter(
    prefix="/api/v1/identity",
    tags=["identity-administration"],
    dependencies=[
        Depends(require_permission(Permission.IDENTITY_ADMIN)),
        Depends(require_feature(Feature.PERSISTENT_IDENTITY)),
    ],
)


def get_identity_administration_service(
    request: Request,
) -> IdentityAdministrationService:
    return request.app.state.container.identity_administration_service


IdentityAdministrationDependency = Annotated[
    IdentityAdministrationService,
    Depends(get_identity_administration_service),
]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)]


class PrincipalContextResponse(BaseModel):
    principal_id: str
    tenant_id: str
    principal_type: PrincipalType
    roles: list[Role]
    authentication_method: str


@router.get("/identity/me", response_model=PrincipalContextResponse)
def get_current_principal(principal: PrincipalDependency) -> PrincipalContextResponse:
    return PrincipalContextResponse(
        principal_id=principal.principal_id,
        tenant_id=principal.tenant_id,
        principal_type=principal.principal_type,
        roles=sorted(principal.roles, key=lambda value: value.value),
        authentication_method=principal.authentication_method,
    )


class CreatePrincipalRequest(BaseModel):
    principal_type: PrincipalType
    display_name: str


class ChangePrincipalStatusRequest(BaseModel):
    status: PrincipalStatus


class PrincipalResponse(BaseModel):
    id: UUID
    tenant_id: str
    principal_type: PrincipalType
    status: PrincipalStatus
    display_name: str
    created_at: datetime
    updated_at: datetime
    revision: int


class AddExternalIdentityRequest(BaseModel):
    issuer: str
    subject: str


class ExternalIdentityResponse(BaseModel):
    id: UUID
    principal_id: UUID
    issuer: str
    subject: str
    created_at: datetime
    created_by: str


class GrantRoleRequest(BaseModel):
    role: Role
    effective_at: datetime | None = None
    expires_at: datetime | None = None


class RevokeRoleRequest(BaseModel):
    reason: str


class RoleBindingResponse(BaseModel):
    id: UUID
    principal_id: UUID
    role: Role
    status: RoleBindingStatus
    effective_at: datetime
    expires_at: datetime | None
    created_at: datetime
    created_by: str
    revoked_at: datetime | None
    revoked_by: str | None
    revoke_reason: str | None
    revision: int


@admin_router.post(
    "/principals", response_model=PrincipalResponse, status_code=status.HTTP_201_CREATED
)
def create_principal(
    request: CreatePrincipalRequest,
    principal: PrincipalDependency,
    service: IdentityAdministrationDependency,
    idempotency_key: IdempotencyKey,
) -> PrincipalResponse:
    return _principal_response(
        service.create_principal(
            principal_type=request.principal_type,
            display_name=request.display_name,
            actor=principal.principal_id,
            idempotency_key=idempotency_key,
        )
    )


@admin_router.get("/principals", response_model=list[PrincipalResponse])
def list_principals(
    service: IdentityAdministrationDependency,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[PrincipalResponse]:
    return [
        _principal_response(value) for value in service.list_principals(limit=limit, offset=offset)
    ]


@admin_router.post("/principals/{principal_id}/status", response_model=PrincipalResponse)
def change_principal_status(
    principal_id: UUID,
    request: ChangePrincipalStatusRequest,
    principal: PrincipalDependency,
    service: IdentityAdministrationDependency,
) -> PrincipalResponse:
    return _principal_response(
        service.change_status(principal_id, status=request.status, actor=principal.principal_id)
    )


@admin_router.post(
    "/principals/{principal_id}/external-identities",
    response_model=ExternalIdentityResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_external_identity(
    principal_id: UUID,
    request: AddExternalIdentityRequest,
    principal: PrincipalDependency,
    service: IdentityAdministrationDependency,
    idempotency_key: IdempotencyKey,
) -> ExternalIdentityResponse:
    value = service.add_external_identity(
        principal_id,
        issuer=request.issuer,
        subject=request.subject,
        actor=principal.principal_id,
        idempotency_key=idempotency_key,
    )
    return _external_identity_response(value)


@admin_router.post(
    "/principals/{principal_id}/role-bindings",
    response_model=RoleBindingResponse,
    status_code=status.HTTP_201_CREATED,
)
def grant_role(
    principal_id: UUID,
    request: GrantRoleRequest,
    principal: PrincipalDependency,
    service: IdentityAdministrationDependency,
    idempotency_key: IdempotencyKey,
) -> RoleBindingResponse:
    return _role_binding_response(
        service.grant_role(
            principal_id,
            role=request.role,
            actor=principal.principal_id,
            effective_at=request.effective_at,
            expires_at=request.expires_at,
            idempotency_key=idempotency_key,
        )
    )


@admin_router.get(
    "/principals/{principal_id}/role-bindings", response_model=list[RoleBindingResponse]
)
def list_role_bindings(
    principal_id: UUID,
    service: IdentityAdministrationDependency,
) -> list[RoleBindingResponse]:
    return [_role_binding_response(value) for value in service.list_role_bindings(principal_id)]


@admin_router.post("/role-bindings/{binding_id}/revoke", response_model=RoleBindingResponse)
def revoke_role(
    binding_id: UUID,
    request: RevokeRoleRequest,
    principal: PrincipalDependency,
    service: IdentityAdministrationDependency,
) -> RoleBindingResponse:
    return _role_binding_response(
        service.revoke_role(binding_id, actor=principal.principal_id, reason=request.reason)
    )


def _principal_response(value: Principal) -> PrincipalResponse:
    return PrincipalResponse(**value.__dict__)


def _external_identity_response(value: ExternalIdentity) -> ExternalIdentityResponse:
    return ExternalIdentityResponse(
        id=value.id,
        principal_id=value.principal_id,
        issuer=value.issuer,
        subject=value.subject,
        created_at=value.created_at,
        created_by=value.created_by,
    )


def _role_binding_response(value: RoleBinding) -> RoleBindingResponse:
    return RoleBindingResponse(
        id=value.id,
        principal_id=value.principal_id,
        role=value.role,
        status=value.status,
        effective_at=value.effective_at,
        expires_at=value.expires_at,
        created_at=value.created_at,
        created_by=value.created_by,
        revoked_at=value.revoked_at,
        revoked_by=value.revoked_by,
        revoke_reason=value.revoke_reason,
        revision=value.revision,
    )
