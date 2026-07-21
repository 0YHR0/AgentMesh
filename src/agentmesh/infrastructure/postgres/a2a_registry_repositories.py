from __future__ import annotations

from dataclasses import asdict
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentmesh.domain.a2a_registry import (
    A2AEndpoint,
    A2APeer,
    A2APeerStatus,
    A2ASkillCandidate,
    A2ATrustTier,
    AgentCardSignatureStatus,
    AgentCardSnapshot,
    AgentCardSource,
)
from agentmesh.infrastructure.postgres.models import A2APeerRecord, AgentCardSnapshotRecord


class SqlAlchemyA2ARegistryRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_peer(self, peer: A2APeer) -> None:
        self._session.add(_peer_record(peer))

    def get_peer(self, peer_id: UUID, *, for_update: bool = False) -> A2APeer | None:
        record = self._session.get(A2APeerRecord, peer_id, with_for_update=for_update)
        return _peer(record) if record is not None else None

    def get_peer_by_name(self, *, tenant_id: str, name: str) -> A2APeer | None:
        record = self._session.scalar(
            select(A2APeerRecord).where(
                A2APeerRecord.tenant_id == tenant_id, A2APeerRecord.name == name
            )
        )
        return _peer(record) if record is not None else None

    def save_peer(self, peer: A2APeer) -> None:
        record = self._session.get(A2APeerRecord, peer.id)
        if record is None:
            raise LookupError(peer.id)
        record.status = peer.status.value
        record.active_card_snapshot_id = peer.active_card_snapshot_id
        record.updated_at = peer.updated_at
        record.revision = peer.revision

    def list_peers(self, *, tenant_id: str, limit: int, offset: int) -> list[A2APeer]:
        records = self._session.scalars(
            select(A2APeerRecord)
            .where(A2APeerRecord.tenant_id == tenant_id)
            .order_by(A2APeerRecord.created_at, A2APeerRecord.id)
            .limit(limit)
            .offset(offset)
        ).all()
        return [_peer(record) for record in records]

    def add_snapshot(self, snapshot: AgentCardSnapshot) -> None:
        self._session.add(_snapshot_record(snapshot))

    def get_snapshot(self, snapshot_id: UUID) -> AgentCardSnapshot | None:
        record = self._session.get(AgentCardSnapshotRecord, snapshot_id)
        return _snapshot(record) if record is not None else None

    def list_snapshots(self, peer_id: UUID) -> list[AgentCardSnapshot]:
        records = self._session.scalars(
            select(AgentCardSnapshotRecord)
            .where(AgentCardSnapshotRecord.peer_id == peer_id)
            .order_by(AgentCardSnapshotRecord.fetched_at.desc(), AgentCardSnapshotRecord.id.desc())
            .limit(20)
        ).all()
        return [_snapshot(record) for record in records]


def _peer_record(value: A2APeer) -> A2APeerRecord:
    return A2APeerRecord(
        id=value.id,
        tenant_id=value.tenant_id,
        owner_id=value.owner_id,
        name=value.name,
        discovery_url=value.discovery_url,
        allowed_endpoint_hosts=list(value.allowed_endpoint_hosts),
        allowed_bindings=list(value.allowed_bindings),
        trust_tier=value.trust_tier.value,
        status=value.status.value,
        active_card_snapshot_id=value.active_card_snapshot_id,
        created_at=value.created_at,
        updated_at=value.updated_at,
        revision=value.revision,
    )


def _peer(value: A2APeerRecord) -> A2APeer:
    return A2APeer(
        id=value.id,
        tenant_id=value.tenant_id,
        owner_id=value.owner_id,
        name=value.name,
        discovery_url=value.discovery_url,
        allowed_endpoint_hosts=tuple(value.allowed_endpoint_hosts),
        allowed_bindings=tuple(value.allowed_bindings),
        trust_tier=A2ATrustTier(value.trust_tier),
        status=A2APeerStatus(value.status),
        active_card_snapshot_id=value.active_card_snapshot_id,
        created_at=value.created_at,
        updated_at=value.updated_at,
        revision=value.revision,
    )


def _snapshot_record(value: AgentCardSnapshot) -> AgentCardSnapshotRecord:
    return AgentCardSnapshotRecord(
        id=value.id,
        tenant_id=value.tenant_id,
        peer_id=value.peer_id,
        digest=value.digest,
        raw_card=dict(value.raw_card),
        agent_name=value.agent_name,
        agent_description=value.agent_description,
        agent_version=value.agent_version,
        endpoints=[asdict(endpoint) for endpoint in value.endpoints],
        skills=[asdict(skill) for skill in value.skills],
        capabilities=dict(value.capabilities),
        security_schemes=dict(value.security_schemes),
        signature_status=value.signature_status.value,
        fetched_at=value.fetched_at,
        expires_at=value.expires_at,
        source_etag=value.source_etag,
        source=value.source.value,
        source_url=value.source_url,
    )


def _snapshot(value: AgentCardSnapshotRecord) -> AgentCardSnapshot:
    return AgentCardSnapshot(
        id=value.id,
        tenant_id=value.tenant_id,
        peer_id=value.peer_id,
        digest=value.digest,
        raw_card=dict(value.raw_card),
        agent_name=value.agent_name,
        agent_description=value.agent_description,
        agent_version=value.agent_version,
        endpoints=tuple(A2AEndpoint(**item) for item in value.endpoints),
        skills=tuple(
            A2ASkillCandidate(
                skill_id=item["skill_id"],
                name=item["name"],
                description=item["description"],
                tags=tuple(item["tags"]),
                input_modes=tuple(item["input_modes"]),
                output_modes=tuple(item["output_modes"]),
            )
            for item in value.skills
        ),
        capabilities=dict(value.capabilities),
        security_schemes=dict(value.security_schemes),
        signature_status=AgentCardSignatureStatus(value.signature_status),
        fetched_at=value.fetched_at,
        expires_at=value.expires_at,
        source_etag=value.source_etag,
        source=AgentCardSource(value.source),
        source_url=value.source_url,
    )
