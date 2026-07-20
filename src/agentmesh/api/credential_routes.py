from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, status
from pydantic import BaseModel, Field

from agentmesh.api.feature_routes import require_feature
from agentmesh.api.security import PrincipalDependency, require_permission
from agentmesh.application.credential_services import CredentialBrokerService
from agentmesh.domain.credentials import (
    CredentialBinding,
    CredentialBindingStatus,
    CredentialLease,
    CredentialLeaseStatus,
    SecretProvider,
    SecretPurpose,
    SecretReference,
    SecretReferenceStatus,
)
from agentmesh.domain.identity import Permission
from agentmesh.features import Feature

router = APIRouter(
    prefix="/api/v1/credentials",
    tags=["credentials"],
    dependencies=[Depends(require_feature(Feature.CREDENTIAL_BROKER))],
)
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)]
ExecutionPermitId = Annotated[UUID | None, Header(alias="Execution-Permit-Id")]


class CreateSecretReferenceRequest(BaseModel):
    provider: SecretProvider
    external_key: str = Field(min_length=1, max_length=255)
    version_selector: str | None = Field(default=None, max_length=128)
    purpose: SecretPurpose
    allowed_audiences: tuple[str, ...] = Field(min_length=1, max_length=32)


class SecretReferenceResponse(BaseModel):
    id: UUID
    tenant_id: str
    provider: SecretProvider
    external_key: str
    version_selector: str | None
    purpose: SecretPurpose
    allowed_audiences: tuple[str, ...]
    status: SecretReferenceStatus
    created_by: str
    created_at: datetime
    updated_at: datetime
    revision: int

    @classmethod
    def from_domain(cls, value: SecretReference) -> SecretReferenceResponse:
        return cls(**value.__dict__)


class BindingTargetRequest(BaseModel):
    workload_principal_id: UUID
    peer_id: UUID
    secret_reference_id: UUID
    environment: str = Field(min_length=1, max_length=64)
    expires_at: datetime


class BindingIntentResponse(BaseModel):
    action_type: str = "credential.binding.create"
    resource_type: str = "a2a_peer"
    resource_id: UUID
    arguments: dict


class CredentialBindingResponse(BaseModel):
    id: UUID
    tenant_id: str
    workload_principal_id: UUID
    peer_id: UUID
    card_snapshot_id: UUID
    card_digest: str
    secret_reference_id: UUID
    scheme_name: str
    auth_scheme: str
    audience: str
    scopes: tuple[str, ...]
    environment: str
    expires_at: datetime
    status: CredentialBindingStatus
    created_by: str
    created_at: datetime
    updated_at: datetime
    revision: int

    @classmethod
    def from_domain(cls, value: CredentialBinding) -> CredentialBindingResponse:
        return cls(**value.__dict__)


class CredentialLeaseResponse(BaseModel):
    id: UUID
    tenant_id: str
    binding_id: UUID
    secret_reference_id: UUID
    workload_principal_id: UUID
    peer_id: UUID
    card_snapshot_id: UUID
    task_id: UUID
    run_id: UUID
    audience: str
    scopes: tuple[str, ...]
    status: CredentialLeaseStatus
    issued_at: datetime | None
    expires_at: datetime
    completed_at: datetime | None
    error: str | None
    created_at: datetime
    updated_at: datetime
    revision: int

    @classmethod
    def from_domain(cls, value: CredentialLease) -> CredentialLeaseResponse:
        return cls(**value.__dict__)


def get_service(request: Request) -> CredentialBrokerService:
    return request.app.state.container.credential_broker_service


ServiceDependency = Annotated[CredentialBrokerService, Depends(get_service)]


@router.post(
    "/secret-references",
    response_model=SecretReferenceResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(Permission.CREDENTIAL_MANAGE))],
)
def create_secret_reference(
    payload: CreateSecretReferenceRequest,
    principal: PrincipalDependency,
    service: ServiceDependency,
    idempotency_key: IdempotencyKey,
) -> SecretReferenceResponse:
    return SecretReferenceResponse.from_domain(
        service.create_secret_reference(
            provider=payload.provider,
            external_key=payload.external_key,
            version_selector=payload.version_selector,
            purpose=payload.purpose,
            allowed_audiences=payload.allowed_audiences,
            principal=principal,
            idempotency_key=idempotency_key,
        )
    )


@router.get(
    "/secret-references",
    response_model=list[SecretReferenceResponse],
    dependencies=[Depends(require_permission(Permission.CREDENTIAL_READ))],
)
def list_secret_references(
    service: ServiceDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[SecretReferenceResponse]:
    return [
        SecretReferenceResponse.from_domain(value)
        for value in service.list_secret_references(limit=limit, offset=offset)
    ]


@router.post(
    "/secret-references/{reference_id}/revoke",
    response_model=SecretReferenceResponse,
    dependencies=[Depends(require_permission(Permission.CREDENTIAL_MANAGE))],
)
def revoke_secret_reference(
    reference_id: UUID, service: ServiceDependency
) -> SecretReferenceResponse:
    return SecretReferenceResponse.from_domain(service.revoke_secret_reference(reference_id))


@router.post(
    "/binding-intents",
    response_model=BindingIntentResponse,
    dependencies=[Depends(require_permission(Permission.CREDENTIAL_MANAGE))],
)
def binding_intent(
    payload: BindingTargetRequest, service: ServiceDependency
) -> BindingIntentResponse:
    value = service.binding_intent(**payload.model_dump())
    return BindingIntentResponse(resource_id=value.peer_id, arguments=value.arguments)


@router.post(
    "/bindings",
    response_model=CredentialBindingResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(Permission.CREDENTIAL_MANAGE))],
)
def create_binding(
    payload: BindingTargetRequest,
    principal: PrincipalDependency,
    service: ServiceDependency,
    idempotency_key: IdempotencyKey,
    permit_id: ExecutionPermitId = None,
) -> CredentialBindingResponse:
    return CredentialBindingResponse.from_domain(
        service.create_binding(
            **payload.model_dump(),
            principal=principal,
            permit_id=permit_id,
            idempotency_key=idempotency_key,
        )
    )


@router.get(
    "/bindings",
    response_model=list[CredentialBindingResponse],
    dependencies=[Depends(require_permission(Permission.CREDENTIAL_READ))],
)
def list_bindings(
    service: ServiceDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[CredentialBindingResponse]:
    return [
        CredentialBindingResponse.from_domain(value)
        for value in service.list_bindings(limit=limit, offset=offset)
    ]


@router.post(
    "/bindings/{binding_id}/revoke",
    response_model=CredentialBindingResponse,
    dependencies=[Depends(require_permission(Permission.CREDENTIAL_MANAGE))],
)
def revoke_binding(binding_id: UUID, service: ServiceDependency) -> CredentialBindingResponse:
    return CredentialBindingResponse.from_domain(service.revoke_binding(binding_id))


@router.get(
    "/leases",
    response_model=list[CredentialLeaseResponse],
    dependencies=[Depends(require_permission(Permission.CREDENTIAL_READ))],
)
def list_leases(
    service: ServiceDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[CredentialLeaseResponse]:
    return [
        CredentialLeaseResponse.from_domain(value)
        for value in service.list_leases(limit=limit, offset=offset)
    ]
