from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, status
from pydantic import BaseModel, Field

from agentmesh.api.feature_routes import require_feature
from agentmesh.api.security import PrincipalDependency, require_permission
from agentmesh.application.a2a_delegation_services import A2ADelegationService
from agentmesh.application.a2a_registry_services import A2APeerView, A2ARegistryService
from agentmesh.domain.a2a_delegation import RemoteCorrelationStatus, RemoteTaskCorrelation
from agentmesh.domain.a2a_registry import (
    A2APeer,
    A2APeerStatus,
    A2ATrustTier,
    AgentCardSignatureStatus,
    AgentCardSnapshot,
    AgentCardSource,
)
from agentmesh.domain.identity import Permission
from agentmesh.features import Feature

router = APIRouter(
    prefix="/api/v1/a2a",
    tags=["a2a-registry"],
    dependencies=[Depends(require_feature(Feature.A2A_FEDERATION))],
)
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)]
ExecutionPermitId = Annotated[UUID | None, Header(alias="Execution-Permit-Id")]


class RegisterPeerRequest(BaseModel):
    owner_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=3, max_length=63)
    discovery_url: str = Field(min_length=1, max_length=2048)
    allowed_endpoint_hosts: list[str] = Field(default_factory=list, max_length=32)
    allowed_bindings: list[str] = Field(min_length=1, max_length=3)
    trust_tier: A2ATrustTier = A2ATrustTier.RESTRICTED


class ImportAgentCardRequest(BaseModel):
    card: dict[str, Any]
    ttl_seconds: int = Field(default=3600, ge=60, le=86_400)
    source_etag: str | None = Field(default=None, max_length=512)


class RevokeAgentCardRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class DelegateTaskRequest(BaseModel):
    peer_id: UUID
    credential_binding_id: UUID | None = None


class CancelDelegationRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class DelegationIntentResponse(BaseModel):
    task_id: UUID
    peer_id: UUID
    action_type: str = "a2a.delegate"
    resource_type: str = "task"
    arguments: dict[str, Any]


class RemoteTaskCorrelationResponse(BaseModel):
    id: UUID
    tenant_id: str
    task_id: UUID
    run_id: UUID
    peer_id: UUID
    card_snapshot_id: UUID
    card_digest: str
    endpoint_url: str
    protocol_binding: str
    protocol_version: str
    endpoint_tenant: str | None
    outbound_message_id: UUID
    request_digest: str
    credential_binding_id: UUID | None
    credential_scheme_name: str | None
    credential_scopes: tuple[str, ...]
    last_credential_lease_id: UUID | None
    status: RemoteCorrelationStatus
    remote_task_id: str | None
    remote_context_id: str | None
    last_remote_state: str | None
    last_response_digest: str | None
    result: dict[str, Any] | None
    error: str | None
    poll_count: int
    poll_failure_count: int
    next_poll_at: datetime | None
    last_polled_at: datetime | None
    poll_lease_owner: str | None
    poll_lease_expires_at: datetime | None
    cancel_requested_at: datetime | None
    cancel_request_count: int
    cancel_request_digest: str | None
    late_result: bool
    created_at: datetime
    updated_at: datetime
    send_started_at: datetime | None
    terminal_at: datetime | None
    revision: int

    @classmethod
    def from_domain(cls, value: RemoteTaskCorrelation) -> RemoteTaskCorrelationResponse:
        return cls(**value.__dict__)


class AgentCardSnapshotResponse(BaseModel):
    id: UUID
    digest: str
    agent_name: str
    agent_description: str
    agent_version: str
    endpoints: list[dict[str, Any]]
    skill_candidates: list[dict[str, Any]]
    capabilities: dict[str, Any]
    security_scheme_names: list[str]
    signature_status: AgentCardSignatureStatus
    fetched_at: datetime
    expires_at: datetime
    source_etag: str | None
    source: AgentCardSource
    source_url: str | None


class A2APeerResponse(BaseModel):
    id: UUID
    tenant_id: str
    owner_id: str
    name: str
    discovery_url: str
    allowed_endpoint_hosts: list[str]
    allowed_bindings: list[str]
    trust_tier: A2ATrustTier
    status: A2APeerStatus
    active_card_snapshot_id: UUID | None
    revision: int
    created_at: datetime
    updated_at: datetime
    card_snapshots: list[AgentCardSnapshotResponse] = Field(default_factory=list)


def get_service(request: Request) -> A2ARegistryService:
    return request.app.state.container.a2a_registry_service


ServiceDependency = Annotated[A2ARegistryService, Depends(get_service)]


def get_delegation_service(request: Request) -> A2ADelegationService:
    return request.app.state.container.a2a_delegation_service


DelegationServiceDependency = Annotated[A2ADelegationService, Depends(get_delegation_service)]


@router.post(
    "/peers",
    response_model=A2APeerResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(Permission.A2A_PEER_MANAGE))],
)
def register_peer(
    payload: RegisterPeerRequest,
    principal: PrincipalDependency,
    service: ServiceDependency,
    idempotency_key: IdempotencyKey,
) -> A2APeerResponse:
    peer = service.register_peer(
        **payload.model_dump(),
        actor=principal.principal_id,
        idempotency_key=idempotency_key,
    )
    return _peer_response(A2APeerView(peer=peer, snapshots=()))


@router.get(
    "/peers",
    response_model=list[A2APeerResponse],
    dependencies=[Depends(require_permission(Permission.A2A_PEER_READ))],
)
def list_peers(
    service: ServiceDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[A2APeerResponse]:
    return [_peer_response(view) for view in service.list_peers(limit=limit, offset=offset)]


@router.post(
    "/peers/{peer_id}/agent-cards",
    response_model=AgentCardSnapshotResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(Permission.A2A_PEER_MANAGE))],
)
def import_agent_card(
    peer_id: UUID,
    payload: ImportAgentCardRequest,
    principal: PrincipalDependency,
    service: ServiceDependency,
    idempotency_key: IdempotencyKey,
) -> AgentCardSnapshotResponse:
    return _snapshot_response(
        service.import_card(
            peer_id,
            **payload.model_dump(),
            actor=principal.principal_id,
            idempotency_key=idempotency_key,
        )
    )


@router.post(
    "/peers/{peer_id}/agent-cards:discover",
    response_model=AgentCardSnapshotResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(Permission.A2A_PEER_MANAGE))],
)
def discover_agent_card(
    peer_id: UUID,
    principal: PrincipalDependency,
    service: ServiceDependency,
    idempotency_key: IdempotencyKey,
) -> AgentCardSnapshotResponse:
    return _snapshot_response(
        service.discover_card(
            peer_id, actor=principal.principal_id, idempotency_key=idempotency_key
        )
    )


@router.post(
    "/peers/{peer_id}/agent-cards/{snapshot_id}:activate",
    response_model=A2APeerResponse,
    dependencies=[Depends(require_permission(Permission.A2A_PEER_MANAGE))],
)
def activate_agent_card(
    peer_id: UUID,
    snapshot_id: UUID,
    principal: PrincipalDependency,
    service: ServiceDependency,
    idempotency_key: IdempotencyKey,
) -> A2APeerResponse:
    return _peer_response(
        A2APeerView(
            peer=service.activate_card(
                peer_id,
                snapshot_id,
                actor=principal.principal_id,
                idempotency_key=idempotency_key,
            ),
            snapshots=(),
        )
    )


@router.get(
    "/peers/{peer_id}/active-agent-card",
    response_model=AgentCardSnapshotResponse,
    dependencies=[Depends(require_permission(Permission.A2A_PEER_READ))],
)
def resolve_active_agent_card(
    peer_id: UUID, service: ServiceDependency
) -> AgentCardSnapshotResponse:
    return _snapshot_response(service.resolve_active_card(peer_id))


@router.post(
    "/peers/{peer_id}/suspend",
    response_model=A2APeerResponse,
    dependencies=[Depends(require_permission(Permission.A2A_PEER_MANAGE))],
)
def suspend_peer(
    peer_id: UUID, principal: PrincipalDependency, service: ServiceDependency
) -> A2APeerResponse:
    return _peer_response(
        A2APeerView(peer=service.suspend_peer(peer_id, actor=principal.principal_id), snapshots=())
    )


@router.post(
    "/peers/{peer_id}/active-agent-card/revoke",
    response_model=A2APeerResponse,
    dependencies=[Depends(require_permission(Permission.A2A_PEER_MANAGE))],
)
def revoke_active_agent_card(
    peer_id: UUID,
    payload: RevokeAgentCardRequest,
    principal: PrincipalDependency,
    service: ServiceDependency,
) -> A2APeerResponse:
    return _peer_response(
        A2APeerView(
            peer=service.revoke_active_card(
                peer_id, actor=principal.principal_id, reason=payload.reason
            ),
            snapshots=(),
        )
    )


@router.get(
    "/tasks/{task_id}/delegation-intent",
    response_model=DelegationIntentResponse,
    dependencies=[
        Depends(require_feature(Feature.A2A_DELEGATION)),
        Depends(require_permission(Permission.A2A_DELEGATE)),
        Depends(require_permission(Permission.TASK_OPERATE)),
    ],
)
def get_delegation_intent(
    task_id: UUID,
    peer_id: UUID,
    service: DelegationServiceDependency,
    credential_binding_id: UUID | None = None,
) -> DelegationIntentResponse:
    value = service.intent(task_id, peer_id, credential_binding_id)
    return DelegationIntentResponse(
        task_id=value.task_id,
        peer_id=value.peer_id,
        arguments=value.arguments,
    )


@router.post(
    "/tasks/{task_id}/delegations",
    response_model=RemoteTaskCorrelationResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        Depends(require_feature(Feature.A2A_DELEGATION)),
        Depends(require_permission(Permission.A2A_DELEGATE)),
        Depends(require_permission(Permission.TASK_OPERATE)),
    ],
)
def delegate_task(
    task_id: UUID,
    payload: DelegateTaskRequest,
    principal: PrincipalDependency,
    service: DelegationServiceDependency,
    idempotency_key: IdempotencyKey,
    permit_id: ExecutionPermitId = None,
) -> RemoteTaskCorrelationResponse:
    return RemoteTaskCorrelationResponse.from_domain(
        service.delegate(
            task_id,
            payload.peer_id,
            principal=principal,
            permit_id=permit_id,
            idempotency_key=idempotency_key,
            credential_binding_id=payload.credential_binding_id,
        )
    )


@router.get(
    "/delegations",
    response_model=list[RemoteTaskCorrelationResponse],
    dependencies=[
        Depends(require_feature(Feature.A2A_DELEGATION)),
        Depends(require_permission(Permission.A2A_PEER_READ)),
    ],
)
def list_delegations(
    service: DelegationServiceDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[RemoteTaskCorrelationResponse]:
    return [
        RemoteTaskCorrelationResponse.from_domain(value)
        for value in service.list(limit=limit, offset=offset)
    ]


@router.get(
    "/delegations/{correlation_id}",
    response_model=RemoteTaskCorrelationResponse,
    dependencies=[
        Depends(require_feature(Feature.A2A_DELEGATION)),
        Depends(require_permission(Permission.A2A_PEER_READ)),
    ],
)
def get_delegation(
    correlation_id: UUID, service: DelegationServiceDependency
) -> RemoteTaskCorrelationResponse:
    return RemoteTaskCorrelationResponse.from_domain(service.get(correlation_id))


@router.post(
    "/delegations/{correlation_id}/reconcile",
    response_model=RemoteTaskCorrelationResponse,
    dependencies=[
        Depends(require_feature(Feature.A2A_DELEGATION)),
        Depends(require_permission(Permission.A2A_DELEGATE)),
        Depends(require_permission(Permission.TASK_OPERATE)),
    ],
)
def reconcile_delegation(
    correlation_id: UUID, service: DelegationServiceDependency
) -> RemoteTaskCorrelationResponse:
    return RemoteTaskCorrelationResponse.from_domain(service.reconcile(correlation_id))


@router.post(
    "/delegations/{correlation_id}/cancel",
    response_model=RemoteTaskCorrelationResponse,
    dependencies=[
        Depends(require_feature(Feature.A2A_DELEGATION)),
        Depends(require_permission(Permission.A2A_DELEGATE)),
        Depends(require_permission(Permission.TASK_OPERATE)),
    ],
)
def cancel_delegation(
    correlation_id: UUID,
    payload: CancelDelegationRequest,
    principal: PrincipalDependency,
    service: DelegationServiceDependency,
    idempotency_key: IdempotencyKey,
) -> RemoteTaskCorrelationResponse:
    return RemoteTaskCorrelationResponse.from_domain(
        service.cancel(
            correlation_id,
            principal=principal,
            idempotency_key=idempotency_key,
            reason=payload.reason,
        )
    )


def _snapshot_response(value: AgentCardSnapshot) -> AgentCardSnapshotResponse:
    return AgentCardSnapshotResponse(
        id=value.id,
        digest=value.digest,
        agent_name=value.agent_name,
        agent_description=value.agent_description,
        agent_version=value.agent_version,
        endpoints=[
            {
                "url": endpoint.url,
                "protocol_binding": endpoint.protocol_binding,
                "protocol_version": endpoint.protocol_version,
                "tenant": endpoint.tenant,
            }
            for endpoint in value.endpoints
        ],
        skill_candidates=[
            {
                "skill_id": skill.skill_id,
                "name": skill.name,
                "description": skill.description,
                "tags": list(skill.tags),
                "input_modes": list(skill.input_modes),
                "output_modes": list(skill.output_modes),
                "verification": "DECLARED_CANDIDATE",
            }
            for skill in value.skills
        ],
        capabilities=value.capabilities,
        security_scheme_names=sorted(value.security_schemes),
        signature_status=value.signature_status,
        fetched_at=value.fetched_at,
        expires_at=value.expires_at,
        source_etag=value.source_etag,
        source=value.source,
        source_url=value.source_url,
    )


def _peer_response(value: A2APeerView) -> A2APeerResponse:
    peer: A2APeer = value.peer
    return A2APeerResponse(
        id=peer.id,
        tenant_id=peer.tenant_id,
        owner_id=peer.owner_id,
        name=peer.name,
        discovery_url=peer.discovery_url,
        allowed_endpoint_hosts=list(peer.allowed_endpoint_hosts),
        allowed_bindings=list(peer.allowed_bindings),
        trust_tier=peer.trust_tier,
        status=peer.status,
        active_card_snapshot_id=peer.active_card_snapshot_id,
        revision=peer.revision,
        created_at=peer.created_at,
        updated_at=peer.updated_at,
        card_snapshots=[_snapshot_response(snapshot) for snapshot in value.snapshots],
    )
