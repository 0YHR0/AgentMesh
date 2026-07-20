from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.domain.a2a_registry import (
    A2APeer,
    A2APeerStatus,
    A2ATrustTier,
    AgentCardSnapshot,
)
from agentmesh.domain.errors import (
    A2ARegistryConflict,
    A2ARegistryNotFound,
    IdempotencyConflict,
)
from agentmesh.domain.messaging import IdempotencyRecord, MessageEnvelope
from agentmesh.domain.tasks import utc_now


@dataclass(frozen=True)
class A2APeerView:
    peer: A2APeer
    snapshots: tuple[AgentCardSnapshot, ...]


class A2ARegistryService:
    def __init__(self, *, uow_factory: UnitOfWorkFactory, tenant_id: str) -> None:
        self._uow_factory = uow_factory
        self._tenant_id = tenant_id

    def register_peer(
        self,
        *,
        owner_id: str,
        name: str,
        discovery_url: str,
        allowed_endpoint_hosts: list[str],
        allowed_bindings: list[str],
        trust_tier: A2ATrustTier,
        actor: str,
        idempotency_key: str,
    ) -> A2APeer:
        candidate = A2APeer.register(
            tenant_id=self._tenant_id,
            owner_id=owner_id,
            name=name,
            discovery_url=discovery_url,
            allowed_endpoint_hosts=allowed_endpoint_hosts,
            allowed_bindings=allowed_bindings,
            trust_tier=trust_tier,
        )
        request_hash = _digest(
            {
                "owner_id": candidate.owner_id,
                "name": candidate.name,
                "discovery_url": candidate.discovery_url,
                "allowed_endpoint_hosts": candidate.allowed_endpoint_hosts,
                "allowed_bindings": candidate.allowed_bindings,
                "trust_tier": candidate.trust_tier.value,
            }
        )
        scope = f"a2a-peer:{self._tenant_id}:{actor}"
        with self._uow_factory() as uow:
            replay = self._replay(uow, scope, idempotency_key, request_hash)
            if replay is not None:
                return self._peer_or_raise(uow, UUID(replay["peer_id"]))
            if uow.a2a_registry.get_peer_by_name(tenant_id=self._tenant_id, name=candidate.name):
                raise A2ARegistryConflict("A2A Peer name already exists")
            uow.a2a_registry.add_peer(candidate)
            self._record(uow, scope, idempotency_key, request_hash, {"peer_id": str(candidate.id)})
            self._event(uow, candidate.id, "agentmesh.a2a.peer-registered", actor)
            uow.commit()
            return candidate

    def import_card(
        self,
        peer_id: UUID,
        *,
        card: dict[str, Any],
        ttl_seconds: int,
        source_etag: str | None,
        actor: str,
        idempotency_key: str,
        max_bytes: int = 262_144,
    ) -> AgentCardSnapshot:
        scope = f"a2a-card:{self._tenant_id}:{actor}"
        request_hash = _digest(
            {
                "peer_id": str(peer_id),
                "card": card,
                "ttl_seconds": ttl_seconds,
                "source_etag": source_etag,
            }
        )
        with self._uow_factory() as uow:
            replay = self._replay(uow, scope, idempotency_key, request_hash)
            if replay is not None:
                snapshot = uow.a2a_registry.get_snapshot(UUID(replay["snapshot_id"]))
                if snapshot is None or snapshot.tenant_id != self._tenant_id:
                    raise A2ARegistryConflict("A2A Card idempotency result was lost")
                return snapshot
            peer = self._peer_or_raise(uow, peer_id, for_update=True)
            candidate = AgentCardSnapshot.import_card(
                tenant_id=self._tenant_id,
                peer=peer,
                raw_card=card,
                ttl_seconds=ttl_seconds,
                source_etag=source_etag,
                max_bytes=max_bytes,
            )
            snapshot = candidate
            uow.a2a_registry.add_snapshot(snapshot)
            peer = peer.select_card(snapshot.id)
            uow.a2a_registry.save_peer(peer)
            self._record(
                uow, scope, idempotency_key, request_hash, {"snapshot_id": str(snapshot.id)}
            )
            self._event(
                uow,
                snapshot.id,
                "agentmesh.a2a.agent-card-imported",
                actor,
                {"peer_id": str(peer.id), "digest": snapshot.digest},
            )
            uow.commit()
            return snapshot

    def suspend_peer(self, peer_id: UUID, *, actor: str) -> A2APeer:
        with self._uow_factory() as uow:
            peer = self._peer_or_raise(uow, peer_id, for_update=True).suspend()
            uow.a2a_registry.save_peer(peer)
            self._event(uow, peer.id, "agentmesh.a2a.peer-suspended", actor)
            uow.commit()
            return peer

    def revoke_active_card(self, peer_id: UUID, *, actor: str, reason: str) -> A2APeer:
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise A2ARegistryConflict("Agent Card revocation requires a reason")
        with self._uow_factory() as uow:
            peer = self._peer_or_raise(uow, peer_id, for_update=True).revoke_active_card()
            uow.a2a_registry.save_peer(peer)
            self._event(
                uow,
                peer.id,
                "agentmesh.a2a.agent-card-revoked",
                actor,
                {"reason": normalized_reason},
            )
            uow.commit()
            return peer

    def list_peers(self, *, limit: int, offset: int) -> list[A2APeerView]:
        with self._uow_factory() as uow:
            peers = uow.a2a_registry.list_peers(
                tenant_id=self._tenant_id, limit=limit, offset=offset
            )
            return [
                A2APeerView(peer=peer, snapshots=tuple(uow.a2a_registry.list_snapshots(peer.id)))
                for peer in peers
            ]

    def resolve_active_card(self, peer_id: UUID) -> AgentCardSnapshot:
        with self._uow_factory() as uow:
            peer = self._peer_or_raise(uow, peer_id)
            if peer.status is not A2APeerStatus.ACTIVE or peer.active_card_snapshot_id is None:
                raise A2ARegistryConflict("A2A Peer has no active Agent Card")
            snapshot = uow.a2a_registry.get_snapshot(peer.active_card_snapshot_id)
            if snapshot is None or snapshot.tenant_id != self._tenant_id:
                raise A2ARegistryConflict("A2A Peer active Agent Card is unavailable")
            if snapshot.expires_at <= utc_now():
                raise A2ARegistryConflict("A2A Peer active Agent Card has expired")
            return snapshot

    def _peer_or_raise(self, uow, peer_id: UUID, *, for_update: bool = False) -> A2APeer:
        peer = uow.a2a_registry.get_peer(peer_id, for_update=for_update)
        if peer is None or peer.tenant_id != self._tenant_id:
            raise A2ARegistryNotFound(f"A2A Peer {peer_id} was not found")
        return peer

    @staticmethod
    def _replay(uow, scope: str, key: str, request_hash: str) -> dict[str, Any] | None:
        normalized_key = key.strip()
        if not normalized_key:
            raise IdempotencyConflict("Idempotency-Key must not be empty")
        uow.idempotency.lock(scope, normalized_key)
        existing = uow.idempotency.get(scope, normalized_key)
        if existing is None:
            return None
        if existing.request_hash != request_hash:
            raise IdempotencyConflict("Idempotency key was reused with a different request")
        return existing.result

    @staticmethod
    def _record(uow, scope: str, key: str, request_hash: str, result: dict[str, Any]) -> None:
        uow.idempotency.add(
            IdempotencyRecord.create(
                scope=scope,
                key=key.strip(),
                request_hash=request_hash,
                result=result,
            )
        )

    def _event(
        self,
        uow,
        aggregate_id: UUID,
        schema_name: str,
        actor: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        uow.outbox.add(
            MessageEnvelope.domain_event(
                schema_name=schema_name,
                tenant_id=self._tenant_id,
                aggregate_id=aggregate_id,
                payload={"actor": actor, **(extra or {})},
            )
        )


def _digest(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()
