import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.application.a2a_registry_services import A2ARegistryService
from agentmesh.config import get_settings
from agentmesh.domain.a2a_registry import A2ATrustTier
from agentmesh.infrastructure.postgres.uow import SqlAlchemyUnitOfWorkFactory

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run service integration tests",
    ),
]


def test_a2a_peer_and_card_snapshot_round_trip_in_postgres() -> None:
    settings = get_settings()
    tenant_id = f"a2a-registry-{uuid4().hex}"
    engine = create_engine(settings.database_url)
    factory = SqlAlchemyUnitOfWorkFactory(
        sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    )
    service = A2ARegistryService(uow_factory=factory, tenant_id=tenant_id)
    try:
        peer = service.register_peer(
            owner_id="federation-team",
            name=f"peer-{uuid4().hex[:12]}",
            discovery_url="https://peer.example/.well-known/agent-card.json",
            allowed_endpoint_hosts=["peer.example"],
            allowed_bindings=["HTTP+JSON"],
            trust_tier=A2ATrustTier.TRUSTED,
            actor="integration-test",
            idempotency_key="peer",
        )
        snapshot = service.import_card(
            peer.id,
            card={
                "name": "PostgreSQL Peer",
                "description": "Integration fixture",
                "supportedInterfaces": [
                    {
                        "url": "https://peer.example/a2a/v1",
                        "protocolBinding": "HTTP+JSON",
                        "protocolVersion": "1.0",
                    }
                ],
                "version": "1.0.0",
                "capabilities": {"streaming": False},
                "defaultInputModes": ["text/plain"],
                "defaultOutputModes": ["text/plain"],
                "skills": [
                    {
                        "id": "echo",
                        "name": "Echo",
                        "description": "Echo one message",
                        "tags": ["echo"],
                    }
                ],
            },
            ttl_seconds=3600,
            source_etag='"integration"',
            actor="integration-test",
            idempotency_key="card",
        )
        resolved = service.resolve_active_card(peer.id)
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT p.status, p.active_card_snapshot_id, c.digest "
                    "FROM a2a_peers p JOIN a2a_agent_card_snapshots c "
                    "ON c.id = p.active_card_snapshot_id WHERE p.id = :peer_id"
                ),
                {"peer_id": peer.id},
            ).one()
        assert resolved.id == snapshot.id
        assert row == ("ACTIVE", snapshot.id, snapshot.digest)
    finally:
        engine.dispose()
