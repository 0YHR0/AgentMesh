from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from agentmesh.application.policy_services import PolicyApprovalService
from agentmesh.application.ports import SecretValueProvider, UnitOfWorkFactory
from agentmesh.domain.a2a_registry import A2APeerStatus, AgentCardSnapshot
from agentmesh.domain.credentials import (
    CredentialBinding,
    CredentialBindingStatus,
    CredentialGrant,
    CredentialLease,
    CredentialLeaseStatus,
    CredentialMaterial,
    SecretProvider,
    SecretPurpose,
    SecretReference,
    SecretReferenceStatus,
)
from agentmesh.domain.errors import (
    CredentialConflict,
    CredentialNotFound,
    CredentialProviderUnavailable,
    IdempotencyConflict,
    InvalidCredential,
)
from agentmesh.domain.identity import PrincipalContext, PrincipalStatus, PrincipalType
from agentmesh.domain.messaging import IdempotencyRecord, MessageEnvelope
from agentmesh.domain.policy import GovernedActionType
from agentmesh.domain.tasks import utc_now


@dataclass(frozen=True)
class CredentialBindingIntent:
    peer_id: UUID
    arguments: dict[str, Any]


@dataclass(frozen=True)
class A2ABearerRequirement:
    scheme_name: str
    scopes: tuple[str, ...]
    audience: str
    card_snapshot_id: UUID
    card_digest: str


class CredentialBrokerService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        tenant_id: str,
        policy_service: PolicyApprovalService,
        provider: SecretValueProvider,
        lease_ttl_seconds: int = 60,
        environment: str = "development",
    ) -> None:
        if lease_ttl_seconds < 1 or lease_ttl_seconds > 300:
            raise InvalidCredential("Credential lease TTL must be between 1 and 300 seconds")
        self._uow_factory = uow_factory
        self._tenant_id = tenant_id
        self._policy = policy_service
        self._provider = provider
        self._lease_ttl_seconds = lease_ttl_seconds
        self._environment = environment.strip().lower()
        if not self._environment or len(self._environment) > 64:
            raise InvalidCredential("Credential Broker environment is invalid")

    def create_secret_reference(
        self,
        *,
        provider: SecretProvider,
        external_key: str,
        version_selector: str | None,
        purpose: SecretPurpose,
        allowed_audiences: tuple[str, ...],
        principal: PrincipalContext,
        idempotency_key: str,
    ) -> SecretReference:
        self._require_principal(principal)
        candidate = SecretReference.create(
            tenant_id=self._tenant_id,
            provider=provider,
            external_key=external_key,
            version_selector=version_selector,
            purpose=purpose,
            allowed_audiences=allowed_audiences,
            created_by=principal.principal_id,
        )
        request_hash = _hash(
            {
                "provider": provider.value,
                "external_key": candidate.external_key,
                "version_selector": candidate.version_selector,
                "purpose": purpose.value,
                "allowed_audiences": list(candidate.allowed_audiences),
            }
        )
        scope = f"credential-reference:{self._tenant_id}:{principal.principal_id}"
        with self._uow_factory() as uow:
            replay = self._replay(uow, scope, idempotency_key, request_hash)
            if replay is not None:
                return self._reference_or_raise(uow, UUID(replay["reference_id"]))
            uow.credentials.add_secret_reference(candidate)
            self._record(
                uow,
                scope,
                idempotency_key,
                request_hash,
                {"reference_id": str(candidate.id)},
            )
            self._event(uow, "agentmesh.credential.secret-reference-created", candidate.id, {})
            uow.commit()
        return candidate

    def binding_intent(
        self,
        *,
        workload_principal_id: UUID,
        peer_id: UUID,
        secret_reference_id: UUID,
        environment: str,
        expires_at: datetime,
    ) -> CredentialBindingIntent:
        with self._uow_factory() as uow:
            arguments = self._binding_arguments(
                uow,
                workload_principal_id=workload_principal_id,
                peer_id=peer_id,
                secret_reference_id=secret_reference_id,
                environment=environment,
                expires_at=expires_at,
            )
        return CredentialBindingIntent(peer_id=peer_id, arguments=arguments)

    def create_binding(
        self,
        *,
        workload_principal_id: UUID,
        peer_id: UUID,
        secret_reference_id: UUID,
        environment: str,
        expires_at: datetime,
        principal: PrincipalContext,
        permit_id: UUID | None,
        idempotency_key: str,
    ) -> CredentialBinding:
        self._require_principal(principal)
        if not self._policy.enabled:
            raise InvalidCredential("CredentialBinding creation requires the Policy service")
        with self._uow_factory() as uow:
            arguments = self._binding_arguments(
                uow,
                workload_principal_id=workload_principal_id,
                peer_id=peer_id,
                secret_reference_id=secret_reference_id,
                environment=environment,
                expires_at=expires_at,
            )
        self._policy.consume_permit(
            permit_id,
            principal=principal,
            action_type=GovernedActionType.CREDENTIAL_BINDING_CREATE,
            resource_type="a2a_peer",
            resource_id=peer_id,
            arguments=arguments,
        )
        request_hash = _hash(arguments)
        scope = f"credential-binding:{self._tenant_id}:{principal.principal_id}"
        with self._uow_factory() as uow:
            replay = self._replay(uow, scope, idempotency_key, request_hash)
            if replay is not None:
                return self._binding_or_raise(uow, UUID(replay["binding_id"]))
            current = self._binding_arguments(
                uow,
                workload_principal_id=workload_principal_id,
                peer_id=peer_id,
                secret_reference_id=secret_reference_id,
                environment=environment,
                expires_at=expires_at,
            )
            if current != arguments:
                raise CredentialConflict(
                    "Credential binding target changed after approval; request a new Permit"
                )
            binding = CredentialBinding.create(
                tenant_id=self._tenant_id,
                workload_principal_id=workload_principal_id,
                peer_id=peer_id,
                card_snapshot_id=UUID(arguments["card_snapshot_id"]),
                card_digest=arguments["card_digest"],
                secret_reference_id=secret_reference_id,
                scheme_name=arguments["scheme_name"],
                auth_scheme=arguments["auth_scheme"],
                audience=arguments["audience"],
                scopes=tuple(arguments["scopes"]),
                environment=environment,
                expires_at=expires_at,
                created_by=principal.principal_id,
            )
            uow.credentials.add_binding(binding)
            self._record(
                uow,
                scope,
                idempotency_key,
                request_hash,
                {"binding_id": str(binding.id)},
            )
            self._event(
                uow,
                "agentmesh.credential.binding-created",
                binding.id,
                {"peer_id": str(peer_id), "workload_principal_id": str(workload_principal_id)},
            )
            uow.commit()
        return binding

    def describe_a2a_binding(
        self,
        binding_id: UUID,
        *,
        workload_principal_id: UUID,
        peer_id: UUID,
        card_snapshot_id: UUID,
        card_digest: str,
        audience: str,
        scheme_name: str,
        scopes: tuple[str, ...],
    ) -> CredentialBinding:
        with self._uow_factory() as uow:
            binding = self._binding_or_raise(uow, binding_id)
            reference = self._reference_or_raise(uow, binding.secret_reference_id)
            self._validate_binding(
                uow,
                binding,
                reference,
                workload_principal_id=workload_principal_id,
                peer_id=peer_id,
                card_snapshot_id=card_snapshot_id,
                card_digest=card_digest,
                audience=audience,
                scheme_name=scheme_name,
                scopes=scopes,
            )
            return binding

    def acquire_for_a2a(
        self,
        binding_id: UUID,
        *,
        workload_principal_id: UUID,
        peer_id: UUID,
        card_snapshot_id: UUID,
        card_digest: str,
        audience: str,
        scheme_name: str,
        scopes: tuple[str, ...],
        task_id: UUID,
        run_id: UUID,
    ) -> CredentialGrant:
        with self._uow_factory() as uow:
            binding = self._binding_or_raise(uow, binding_id)
            reference = self._reference_or_raise(uow, binding.secret_reference_id)
            self._validate_binding(
                uow,
                binding,
                reference,
                workload_principal_id=workload_principal_id,
                peer_id=peer_id,
                card_snapshot_id=card_snapshot_id,
                card_digest=card_digest,
                audience=audience,
                scheme_name=scheme_name,
                scopes=scopes,
            )
            lease = CredentialLease.request(
                tenant_id=self._tenant_id,
                binding=binding,
                task_id=task_id,
                run_id=run_id,
                ttl_seconds=self._lease_ttl_seconds,
            )
            uow.credentials.add_lease(lease)
            self._event(
                uow,
                "agentmesh.credential.lease-requested",
                lease.id,
                {"binding_id": str(binding.id), "run_id": str(run_id)},
            )
            uow.commit()
        try:
            value = self._provider.resolve(reference)
        except CredentialProviderUnavailable:
            self._fail_requested_lease(lease.id, "provider_unavailable")
            raise
        try:
            with self._uow_factory() as uow:
                current_binding = self._binding_or_raise(uow, binding_id, for_update=True)
                current_reference = self._reference_or_raise(
                    uow, current_binding.secret_reference_id, for_update=True
                )
                self._validate_binding(
                    uow,
                    current_binding,
                    current_reference,
                    workload_principal_id=workload_principal_id,
                    peer_id=peer_id,
                    card_snapshot_id=card_snapshot_id,
                    card_digest=card_digest,
                    audience=audience,
                    scheme_name=scheme_name,
                    scopes=scopes,
                )
                current = self._lease_or_raise(uow, lease.id, for_update=True)
                issued = current.issue()
                uow.credentials.save_lease(issued)
                self._event(uow, "agentmesh.credential.lease-issued", issued.id, {})
                uow.commit()
        except (CredentialConflict, CredentialNotFound, InvalidCredential):
            self._fail_requested_lease(lease.id, "binding_changed_before_issuance")
            raise
        return CredentialGrant(
            lease=issued,
            material=CredentialMaterial(
                lease_id=issued.id,
                auth_scheme=binding.auth_scheme,
                value=value,
            ),
        )

    def settle_lease(
        self, lease_id: UUID, *, used: bool, error: str | None = None
    ) -> CredentialLease:
        with self._uow_factory() as uow:
            lease = self._lease_or_raise(uow, lease_id, for_update=True)
            if lease.status in {CredentialLeaseStatus.USED, CredentialLeaseStatus.FAILED}:
                return lease
            settled = lease.settle(used=used, error=error)
            uow.credentials.save_lease(settled)
            self._event(
                uow,
                "agentmesh.credential.lease-settled",
                settled.id,
                {"status": settled.status.value},
            )
            uow.commit()
            return settled

    def revoke_secret_reference(self, reference_id: UUID) -> SecretReference:
        with self._uow_factory() as uow:
            reference = self._reference_or_raise(uow, reference_id, for_update=True)
            updated = reference.revoke()
            uow.credentials.save_secret_reference(updated)
            self._event(uow, "agentmesh.credential.secret-reference-revoked", updated.id, {})
            uow.commit()
            return updated

    def revoke_binding(self, binding_id: UUID) -> CredentialBinding:
        with self._uow_factory() as uow:
            binding = self._binding_or_raise(uow, binding_id, for_update=True)
            updated = binding.revoke()
            uow.credentials.save_binding(updated)
            self._event(uow, "agentmesh.credential.binding-revoked", updated.id, {})
            uow.commit()
            return updated

    def list_secret_references(self, *, limit: int, offset: int) -> list[SecretReference]:
        with self._uow_factory() as uow:
            return uow.credentials.list_secret_references(
                tenant_id=self._tenant_id, limit=limit, offset=offset
            )

    def list_bindings(self, *, limit: int, offset: int) -> list[CredentialBinding]:
        with self._uow_factory() as uow:
            return uow.credentials.list_bindings(
                tenant_id=self._tenant_id, limit=limit, offset=offset
            )

    def list_leases(self, *, limit: int, offset: int) -> list[CredentialLease]:
        with self._uow_factory() as uow:
            return uow.credentials.list_leases(
                tenant_id=self._tenant_id, limit=limit, offset=offset
            )

    def _binding_arguments(
        self,
        uow,
        *,
        workload_principal_id: UUID,
        peer_id: UUID,
        secret_reference_id: UUID,
        environment: str,
        expires_at: datetime,
    ) -> dict[str, Any]:
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise InvalidCredential("CredentialBinding expiry must include a UTC offset")
        workload = uow.identity.get_principal(workload_principal_id)
        if (
            workload is None
            or workload.tenant_id != self._tenant_id
            or workload.principal_type is not PrincipalType.SERVICE
            or workload.status is not PrincipalStatus.ACTIVE
        ):
            raise InvalidCredential(
                "Credential workload must be an active tenant SERVICE Principal"
            )
        reference = self._reference_or_raise(uow, secret_reference_id)
        if reference.status is not SecretReferenceStatus.ACTIVE:
            raise InvalidCredential("SecretReference is not active")
        if reference.purpose is not SecretPurpose.A2A_HTTP_BEARER:
            raise InvalidCredential("SecretReference purpose is incompatible with A2A Bearer")
        requirement = self._a2a_requirement(uow, peer_id)
        if requirement.audience not in reference.allowed_audiences:
            raise InvalidCredential("SecretReference does not allow the A2A audience")
        return {
            "workload_principal_id": str(workload_principal_id),
            "peer_id": str(peer_id),
            "card_snapshot_id": str(requirement.card_snapshot_id),
            "card_digest": requirement.card_digest,
            "secret_reference_id": str(secret_reference_id),
            "scheme_name": requirement.scheme_name,
            "auth_scheme": "Bearer",
            "audience": requirement.audience,
            "scopes": list(requirement.scopes),
            "environment": environment.strip().lower(),
            "expires_at": expires_at.isoformat(),
        }

    def _a2a_requirement(self, uow, peer_id: UUID) -> A2ABearerRequirement:
        peer = uow.a2a_registry.get_peer(peer_id)
        if (
            peer is None
            or peer.tenant_id != self._tenant_id
            or peer.status is not A2APeerStatus.ACTIVE
            or peer.active_card_snapshot_id is None
        ):
            raise InvalidCredential("A2A Peer is not active for this tenant")
        snapshot = uow.a2a_registry.get_snapshot(peer.active_card_snapshot_id)
        if snapshot is None or snapshot.tenant_id != self._tenant_id:
            raise InvalidCredential("A2A Agent Card snapshot is unavailable")
        if snapshot.expires_at <= utc_now():
            raise InvalidCredential("A2A Agent Card snapshot has expired")
        endpoint = next(
            (
                item
                for item in snapshot.endpoints
                if item.protocol_binding == "HTTP+JSON" and item.protocol_version == "1.0"
            ),
            None,
        )
        if endpoint is None:
            raise InvalidCredential("Peer has no supported A2A 1.0 HTTP+JSON interface")
        scheme_name, scopes = bearer_requirement(snapshot)
        return A2ABearerRequirement(
            scheme_name=scheme_name,
            scopes=scopes,
            audience=endpoint.url.rstrip("/"),
            card_snapshot_id=snapshot.id,
            card_digest=snapshot.digest,
        )

    def _validate_binding(
        self,
        uow,
        binding: CredentialBinding,
        reference: SecretReference,
        **expected,
    ) -> None:
        now = utc_now()
        if binding.environment != self._environment:
            raise CredentialConflict("CredentialBinding is for another environment")
        if binding.status is not CredentialBindingStatus.ACTIVE or binding.expires_at <= now:
            raise CredentialConflict("CredentialBinding is inactive or expired")
        if reference.status is not SecretReferenceStatus.ACTIVE:
            raise CredentialConflict("SecretReference is inactive")
        if binding.tenant_id != self._tenant_id or reference.tenant_id != self._tenant_id:
            raise CredentialNotFound("Credential binding was not found")
        for field, value in expected.items():
            current = getattr(binding, field)
            if field == "scopes":
                value = tuple(sorted(value))
            if current != value:
                raise CredentialConflict(f"CredentialBinding {field} does not match the request")
        workload = uow.identity.get_principal(binding.workload_principal_id)
        if (
            workload is None
            or workload.tenant_id != self._tenant_id
            or workload.principal_type is not PrincipalType.SERVICE
            or workload.status is not PrincipalStatus.ACTIVE
        ):
            raise CredentialConflict("Credential workload Principal is inactive")
        if binding.audience not in reference.allowed_audiences:
            raise CredentialConflict("SecretReference no longer allows the binding audience")

    def _fail_requested_lease(self, lease_id: UUID, error: str) -> None:
        with self._uow_factory() as uow:
            lease = self._lease_or_raise(uow, lease_id, for_update=True)
            failed = lease.fail_request(error)
            uow.credentials.save_lease(failed)
            self._event(uow, "agentmesh.credential.lease-failed", failed.id, {})
            uow.commit()

    def _reference_or_raise(self, uow, reference_id: UUID, *, for_update: bool = False):
        value = uow.credentials.get_secret_reference(reference_id, for_update=for_update)
        if value is None or value.tenant_id != self._tenant_id:
            raise CredentialNotFound(f"SecretReference {reference_id} was not found")
        return value

    def _binding_or_raise(self, uow, binding_id: UUID, *, for_update: bool = False):
        value = uow.credentials.get_binding(binding_id, for_update=for_update)
        if value is None or value.tenant_id != self._tenant_id:
            raise CredentialNotFound(f"CredentialBinding {binding_id} was not found")
        return value

    def _lease_or_raise(self, uow, lease_id: UUID, *, for_update: bool = False):
        value = uow.credentials.get_lease(lease_id, for_update=for_update)
        if value is None or value.tenant_id != self._tenant_id:
            raise CredentialNotFound(f"CredentialLease {lease_id} was not found")
        return value

    def _require_principal(self, principal: PrincipalContext) -> None:
        if not principal.authenticated or principal.tenant_id != self._tenant_id:
            raise InvalidCredential("Credential administration requires a tenant Principal")

    @staticmethod
    def _replay(uow, scope: str, key: str, request_hash: str):
        normalized = key.strip()
        if not normalized:
            raise IdempotencyConflict("Idempotency-Key must not be empty")
        uow.idempotency.lock(scope, normalized)
        existing = uow.idempotency.get(scope, normalized)
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
        schema_name: str,
        aggregate_id: UUID,
        payload: dict[str, Any],
    ) -> None:
        uow.outbox.add(
            MessageEnvelope.domain_event(
                schema_name=schema_name,
                tenant_id=self._tenant_id,
                aggregate_id=aggregate_id,
                payload={"credential_record_id": str(aggregate_id), **payload},
            )
        )


def bearer_requirement(snapshot: AgentCardSnapshot) -> tuple[str, tuple[str, ...]]:
    raw = snapshot.raw_card
    requirements = raw.get("securityRequirements", raw.get("security", []))
    schemes = raw.get("securitySchemes", {})
    if not isinstance(requirements, list) or not requirements or not isinstance(schemes, dict):
        raise InvalidCredential("A2A Agent Card does not declare an authentication requirement")
    for requirement in requirements:
        if not isinstance(requirement, dict) or len(requirement) != 1:
            continue
        scheme_name, scopes = next(iter(requirement.items()))
        definition = schemes.get(scheme_name)
        if not isinstance(scheme_name, str) or not isinstance(scopes, list):
            continue
        if not all(isinstance(scope, str) and scope.strip() for scope in scopes):
            continue
        if not isinstance(definition, dict):
            continue
        http = definition.get("httpAuthSecurityScheme")
        if isinstance(http, dict) and str(http.get("scheme", "")).lower() == "bearer":
            return scheme_name, tuple(sorted(set(scope.strip() for scope in scopes)))
    raise InvalidCredential("This baseline supports only a simple A2A HTTP Bearer requirement")


def _hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()
