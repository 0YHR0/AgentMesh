import json
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient

from agentmesh.api.app import create_app
from agentmesh.application.a2a_registry_services import A2ARegistryService
from agentmesh.application.identity_services import IdentityService
from agentmesh.application.ports import AgentCardFetchResult
from agentmesh.bootstrap import ApplicationContainer
from agentmesh.domain.a2a_registry import A2APeerStatus, A2ATrustTier, AgentCardSource
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


class _DiscoveryClient:
    def __init__(self, results: list[AgentCardFetchResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, str | None]] = []

    def fetch_agent_card(self, *, discovery_url: str, source_etag: str | None = None):
        self.calls.append((discovery_url, source_etag))
        return self.results.pop(0)


def _registry(
    discovery_client=None,
) -> tuple[InMemoryUnitOfWorkFactory, A2ARegistryService]:
    factory = InMemoryUnitOfWorkFactory()
    return factory, A2ARegistryService(
        uow_factory=factory,
        tenant_id="test-tenant",
        discovery_client=discovery_client,
    )


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


def test_discovery_creates_candidate_until_explicit_idempotent_activation() -> None:
    client = _DiscoveryClient(
        [
            AgentCardFetchResult(
                card=_card(),
                source_etag='"network-v1"',
                cache_max_age_seconds=900,
            )
        ]
    )
    factory, service = _registry(client)
    peer = _peer(service)

    candidate = service.discover_card(peer.id, actor="operator", idempotency_key="discover-v1")
    replay = service.discover_card(peer.id, actor="operator", idempotency_key="discover-v1")

    assert replay.id == candidate.id
    assert candidate.source is AgentCardSource.DISCOVERED
    assert candidate.source_url == peer.discovery_url
    assert candidate.source_etag == '"network-v1"'
    assert candidate.expires_at - candidate.fetched_at == timedelta(seconds=900)
    assert service.list_peers(limit=1, offset=0)[0].peer.status is A2APeerStatus.REGISTERED
    assert service.list_peers(limit=1, offset=0)[0].peer.active_card_snapshot_id is None
    assert client.calls == [(peer.discovery_url, None)]

    activated = service.activate_card(
        peer.id,
        candidate.id,
        actor="operator",
        idempotency_key="activate-v1",
    )
    activation_replay = service.activate_card(
        peer.id,
        candidate.id,
        actor="operator",
        idempotency_key="activate-v1",
    )
    assert activated.active_card_snapshot_id == candidate.id
    assert activation_replay.active_card_snapshot_id == candidate.id
    assert activated.status is A2APeerStatus.ACTIVE
    assert len(factory.store.outbox) == 3


def test_discovery_uses_etag_and_materializes_304_as_new_immutable_snapshot() -> None:
    client = _DiscoveryClient(
        [
            AgentCardFetchResult(_card(), '"v1"', 120),
            AgentCardFetchResult(None, '"v1"', 300, not_modified=True),
        ]
    )
    _, service = _registry(client)
    peer = _peer(service)
    first = service.discover_card(peer.id, actor="operator", idempotency_key="fetch-1")
    second = service.discover_card(peer.id, actor="operator", idempotency_key="fetch-2")

    assert second.id != first.id
    assert second.digest == first.digest
    assert second.raw_card == first.raw_card
    assert second.expires_at - second.fetched_at == timedelta(seconds=300)
    assert client.calls == [(peer.discovery_url, None), (peer.discovery_url, '"v1"')]


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
    _, api_registry = _registry(
        _DiscoveryClient([AgentCardFetchResult(_card(), '"api-discovered"', 600)])
    )
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
        a2a_registry_service=api_registry,
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
        manual_snapshot_id = card_response.json()["id"]
        discovered = client.post(
            f"/api/v1/a2a/peers/{peer_id}/agent-cards:discover",
            headers={**headers, "Idempotency-Key": "api-discovery"},
        )
        assert discovered.status_code == 201
        assert discovered.json()["source"] == "DISCOVERED"
        peers = client.get("/api/v1/a2a/peers", headers=headers).json()
        assert peers[0]["active_card_snapshot_id"] == manual_snapshot_id
        activated = client.post(
            f"/api/v1/a2a/peers/{peer_id}/agent-cards/{discovered.json()['id']}:activate",
            headers={**headers, "Idempotency-Key": "api-activation"},
        )
        assert activated.status_code == 200
        assert activated.json()["active_card_snapshot_id"] == discovered.json()["id"]
        assert client.get("/api/v1/a2a/peers", headers=headers).status_code == 200
