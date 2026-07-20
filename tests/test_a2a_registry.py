import json
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient

from agentmesh.api.app import create_app
from agentmesh.application.a2a_registry_services import A2ARegistryService
from agentmesh.application.identity_services import IdentityService
from agentmesh.bootstrap import ApplicationContainer
from agentmesh.domain.a2a_registry import A2APeerStatus, A2ATrustTier
from agentmesh.domain.errors import (
    A2ARegistryConflict,
    A2ARegistryNotFound,
    IdempotencyConflict,
    InvalidA2ARegistry,
)
from agentmesh.domain.identity import Role
from agentmesh.features import FeatureGateSet
from tests.fakes import InMemoryUnitOfWorkFactory

TOKEN = "federation-token-000000000000000000000000000"


def _card(
    *,
    endpoint: str = "https://peer.example/a2a/v1",
    protocol_version: str = "1.0",
) -> dict:
    return {
        "name": "Research Agent",
        "description": "Researches bounded topics.",
        "supportedInterfaces": [
            {
                "url": endpoint,
                "protocolBinding": "HTTP+JSON",
                "protocolVersion": protocol_version,
            }
        ],
        "version": "2.1.0",
        "capabilities": {"streaming": True, "pushNotifications": False},
        "securitySchemes": {"oauth": {"oauth2SecurityScheme": {}}},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["application/json"],
        "skills": [
            {
                "id": "research",
                "name": "Research",
                "description": "Collect evidence.",
                "tags": ["research", "citations"],
            }
        ],
        "x-future-field": {"preserved": True},
    }


def _registry() -> tuple[InMemoryUnitOfWorkFactory, A2ARegistryService]:
    factory = InMemoryUnitOfWorkFactory()
    return factory, A2ARegistryService(uow_factory=factory, tenant_id="test-tenant")


def _peer(service: A2ARegistryService, *, key: str = "peer-1"):
    return service.register_peer(
        owner_id="platform-team",
        name="research-peer",
        discovery_url="https://peer.example/.well-known/agent-card.json",
        allowed_endpoint_hosts=["peer.example"],
        allowed_bindings=["HTTP+JSON"],
        trust_tier=A2ATrustTier.TRUSTED,
        actor="federation-operator",
        idempotency_key=key,
    )


def test_card_import_is_content_addressed_immutable_and_resolvable() -> None:
    factory, service = _registry()
    peer = _peer(service)

    snapshot = service.import_card(
        peer.id,
        card=_card(),
        ttl_seconds=3600,
        source_etag='"card-v1"',
        actor="federation-operator",
        idempotency_key="card-1",
    )
    replay = service.import_card(
        peer.id,
        card=_card(),
        ttl_seconds=3600,
        source_etag='"card-v1"',
        actor="federation-operator",
        idempotency_key="card-1",
    )
    same_content = service.import_card(
        peer.id,
        card=_card(),
        ttl_seconds=3600,
        source_etag='"card-v1"',
        actor="federation-operator",
        idempotency_key="card-2",
    )

    assert replay.id == snapshot.id
    assert same_content.id != snapshot.id
    assert same_content.digest == snapshot.digest
    assert snapshot.digest.startswith("sha256:")
    assert snapshot.raw_card["x-future-field"] == {"preserved": True}
    assert snapshot.skills[0].skill_id == "research"
    assert service.resolve_active_card(peer.id).id == same_content.id
    view = service.list_peers(limit=10, offset=0)[0]
    assert view.peer.status is A2APeerStatus.ACTIVE
    assert view.peer.active_card_snapshot_id == same_content.id
    assert len(view.snapshots) == 2
    assert len(factory.store.outbox) == 3


def test_card_import_fails_closed_for_endpoint_protocol_and_request_reuse() -> None:
    _, service = _registry()
    peer = _peer(service)

    with pytest.raises(InvalidA2ARegistry, match="not allowed"):
        service.import_card(
            peer.id,
            card=_card(endpoint="https://attacker.example/a2a"),
            ttl_seconds=3600,
            source_etag=None,
            actor="operator",
            idempotency_key="bad-host",
        )
    with pytest.raises(InvalidA2ARegistry, match="unsupported"):
        service.import_card(
            peer.id,
            card=_card(protocol_version="0.3"),
            ttl_seconds=3600,
            source_etag=None,
            actor="operator",
            idempotency_key="bad-version",
        )
    with pytest.raises(InvalidA2ARegistry, match="credential material"):
        service.import_card(
            peer.id,
            card={**_card(), "x-auth": {"clientSecret": "must-not-persist"}},
            ttl_seconds=3600,
            source_etag=None,
            actor="operator",
            idempotency_key="secret-card",
        )
    service.import_card(
        peer.id,
        card=_card(),
        ttl_seconds=3600,
        source_etag=None,
        actor="operator",
        idempotency_key="stable-key",
    )
    with pytest.raises(IdempotencyConflict):
        service.import_card(
            peer.id,
            card={**_card(), "version": "2.2.0"},
            ttl_seconds=3600,
            source_etag=None,
            actor="operator",
            idempotency_key="stable-key",
        )


def test_suspension_revocation_expiry_and_tenant_isolation_fail_closed() -> None:
    factory, service = _registry()
    peer = _peer(service)
    service.import_card(
        peer.id,
        card=_card(),
        ttl_seconds=3600,
        source_etag=None,
        actor="operator",
        idempotency_key="card",
    )
    service.suspend_peer(peer.id, actor="operator")
    with pytest.raises(A2ARegistryConflict, match="no active"):
        service.resolve_active_card(peer.id)

    refreshed = service.import_card(
        peer.id,
        card=_card(),
        ttl_seconds=3600,
        source_etag=None,
        actor="operator",
        idempotency_key="reactivate",
    )
    factory.store.a2a_card_snapshots[refreshed.id] = replace(
        refreshed, expires_at=refreshed.fetched_at - timedelta(seconds=1)
    )
    with pytest.raises(A2ARegistryConflict, match="expired"):
        service.resolve_active_card(peer.id)

    factory.store.a2a_card_snapshots[refreshed.id] = refreshed
    revoked = service.revoke_active_card(peer.id, actor="operator", reason="trust withdrawn")
    assert revoked.status is A2APeerStatus.REGISTERED
    assert revoked.active_card_snapshot_id is None
    assert len(factory.store.a2a_card_snapshots) == 2

    other = A2ARegistryService(uow_factory=factory, tenant_id="other-tenant")
    with pytest.raises(A2ARegistryNotFound, match="not found"):
        other.resolve_active_card(peer.id)


def test_a2a_api_is_feature_gated_and_rbac_protected(
    application_container: ApplicationContainer,
) -> None:
    with TestClient(create_app(application_container)) as disabled:
        assert disabled.get("/api/v1/a2a/peers").status_code == 403

    principal = {
        "principal_id": "federation-operator",
        "tenant_id": "test-tenant",
        "principal_type": "USER",
        "roles": [Role.FEDERATION_OPERATOR.value],
        "token_sha256": sha256(TOKEN.encode()).hexdigest(),
    }
    secured = replace(
        application_container,
        feature_gates=FeatureGateSet.from_config(
            "minimal", "identity_rbac=true,a2a_federation=true"
        ),
        identity_service=IdentityService(
            enabled=True,
            tenant_id="test-tenant",
            principals_json=json.dumps([principal]),
        ),
    )
    with TestClient(create_app(secured)) as client:
        headers = {"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": "api-peer"}
        response = client.post(
            "/api/v1/a2a/peers",
            headers=headers,
            json={
                "owner_id": "platform-team",
                "name": "research-peer",
                "discovery_url": "https://peer.example/.well-known/agent-card.json",
                "allowed_endpoint_hosts": [],
                "allowed_bindings": ["HTTP+JSON"],
                "trust_tier": "TRUSTED",
            },
        )
        assert response.status_code == 201
        peer_id = response.json()["id"]
        card_response = client.post(
            f"/api/v1/a2a/peers/{peer_id}/agent-cards",
            headers={**headers, "Idempotency-Key": "api-card"},
            json={"card": _card(), "ttl_seconds": 3600},
        )
        assert card_response.status_code == 201
        assert card_response.json()["skill_candidates"][0]["verification"] == "DECLARED_CANDIDATE"
        assert client.get("/api/v1/a2a/peers", headers=headers).status_code == 200
