from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import UUID

from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.domain.errors import (
    AuthenticationFailed,
    AuthenticationRequired,
    AuthorizationDenied,
    IdempotencyConflict,
    IdentityConflict,
    InvalidIdentity,
    InvalidIdentityConfiguration,
    PrincipalNotFound,
    RoleBindingNotFound,
)
from agentmesh.domain.identity import (
    ExternalIdentity,
    Permission,
    Principal,
    PrincipalContext,
    PrincipalStatus,
    PrincipalType,
    Role,
    RoleBinding,
)
from agentmesh.domain.messaging import IdempotencyRecord, MessageEnvelope
from agentmesh.integrations.oidc import VerifiedOidcIdentity


class OidcVerifier(Protocol):
    def verify(self, token: str) -> VerifiedOidcIdentity: ...


@dataclass(frozen=True)
class ConfiguredPrincipal:
    principal_id: str
    tenant_id: str
    principal_type: PrincipalType
    status: PrincipalStatus
    roles: frozenset[Role]
    token_sha256: str
    expires_at: datetime | None = None


class IdentityService:
    def __init__(
        self,
        *,
        enabled: bool,
        tenant_id: str,
        principals_json: str = "[]",
        persistent: bool = False,
        uow_factory: UnitOfWorkFactory | None = None,
        oidc_verifier: OidcVerifier | None = None,
    ) -> None:
        self.enabled = enabled
        self.persistent = persistent
        self.tenant_id = tenant_id
        self._principals = self._parse_principals(principals_json) if enabled else ()
        self._uow_factory = uow_factory
        self._oidc_verifier = oidc_verifier
        if enabled and not self._principals and oidc_verifier is None:
            raise InvalidIdentityConfiguration(
                "identity_rbac requires at least one configured Principal or an OIDC verifier"
            )
        if persistent and (not enabled or uow_factory is None):
            raise InvalidIdentityConfiguration(
                "persistent identity requires identity_rbac and a UnitOfWork factory"
            )

    @property
    def configured_principals(self) -> tuple[ConfiguredPrincipal, ...]:
        return self._principals

    def authenticate(self, authorization: str | None) -> PrincipalContext:
        if not self.enabled:
            return self.local_context()
        token = self._bearer_token(authorization)
        matched = self._match_configured(token)
        if not self.persistent:
            if matched is None:
                raise AuthenticationFailed("Invalid bearer credential")
            self._validate_configured(matched)
            return self._configured_context(matched)

        if matched is not None:
            self._validate_configured(matched)
            try:
                principal_id = UUID(matched.principal_id)
            except ValueError as exc:
                raise AuthenticationFailed(
                    "Persistent bootstrap Principal ID must be a UUID"
                ) from exc
            return self._persistent_context(principal_id, "bearer_sha256")
        if self._oidc_verifier is None:
            raise AuthenticationFailed("Invalid bearer credential")
        verified = self._oidc_verifier.verify(token)
        assert self._uow_factory is not None
        with self._uow_factory() as uow:
            external = uow.identity.get_external_identity(
                tenant_id=self.tenant_id,
                issuer=verified.issuer,
                subject=verified.subject,
            )
        if external is None:
            raise AuthenticationFailed("OIDC identity is not registered")
        return self._persistent_context(external.principal_id, "oidc")

    def authorize(self, principal: PrincipalContext, permission: Permission) -> None:
        if principal.tenant_id != self.tenant_id or permission not in principal.permissions:
            raise AuthorizationDenied(
                f"Principal '{principal.principal_id}' lacks permission '{permission.value}'"
            )

    def local_context(self) -> PrincipalContext:
        return PrincipalContext(
            principal_id="local-development",
            tenant_id=self.tenant_id,
            principal_type=PrincipalType.SERVICE,
            roles=frozenset({Role.TENANT_ADMIN}),
            authenticated=False,
            authentication_method="feature_disabled",
        )

    def _persistent_context(self, principal_id: UUID, method: str) -> PrincipalContext:
        assert self._uow_factory is not None
        now = datetime.now(timezone.utc)
        with self._uow_factory() as uow:
            principal = uow.identity.get_principal(principal_id)
            bindings = uow.identity.list_role_bindings(principal_id)
        if (
            principal is None
            or principal.tenant_id != self.tenant_id
            or principal.status is not PrincipalStatus.ACTIVE
        ):
            raise AuthenticationFailed("Principal is not active")
        roles = frozenset(binding.role for binding in bindings if binding.is_effective(now))
        if not roles:
            raise AuthenticationFailed("Principal has no active role binding")
        return PrincipalContext(
            principal_id=str(principal.id),
            tenant_id=principal.tenant_id,
            principal_type=principal.principal_type,
            roles=roles,
            authenticated=True,
            authentication_method=method,
        )

    @staticmethod
    def _bearer_token(authorization: str | None) -> str:
        if authorization is None:
            raise AuthenticationRequired("Bearer authentication is required")
        scheme, separator, token = authorization.partition(" ")
        if separator != " " or scheme.lower() != "bearer" or not token or " " in token:
            raise AuthenticationFailed("Invalid bearer credential")
        if len(token.encode("utf-8")) < 32:
            raise AuthenticationFailed("Invalid bearer credential")
        return token

    def _match_configured(self, token: str) -> ConfiguredPrincipal | None:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        matched = None
        for principal in self._principals:
            if hmac.compare_digest(principal.token_sha256, digest):
                matched = principal
        return matched

    def _validate_configured(self, principal: ConfiguredPrincipal) -> None:
        now = datetime.now(timezone.utc)
        if principal.status is not PrincipalStatus.ACTIVE:
            raise AuthenticationFailed("Principal is not active")
        if principal.expires_at is not None and principal.expires_at <= now:
            raise AuthenticationFailed("Bearer credential has expired")
        if principal.tenant_id != self.tenant_id:
            raise AuthenticationFailed("Principal tenant does not match this control plane")

    @staticmethod
    def _configured_context(principal: ConfiguredPrincipal) -> PrincipalContext:
        return PrincipalContext(
            principal_id=principal.principal_id,
            tenant_id=principal.tenant_id,
            principal_type=principal.principal_type,
            roles=principal.roles,
            authenticated=True,
            authentication_method="bearer_sha256",
        )

    @classmethod
    def _parse_principals(cls, value: str) -> tuple[ConfiguredPrincipal, ...]:
        try:
            raw = json.loads(value)
        except json.JSONDecodeError as exc:
            raise InvalidIdentityConfiguration(
                "identity_principals_json must be valid JSON"
            ) from exc
        if not isinstance(raw, list):
            raise InvalidIdentityConfiguration("identity_principals_json must be a JSON array")
        principals = tuple(cls._parse_principal(item) for item in raw)
        identifiers = [principal.principal_id for principal in principals]
        digests = [principal.token_sha256 for principal in principals]
        if len(set(identifiers)) != len(identifiers):
            raise InvalidIdentityConfiguration("Principal IDs must be unique")
        if len(set(digests)) != len(digests):
            raise InvalidIdentityConfiguration("Bearer token digests must be unique")
        return principals

    @staticmethod
    def _parse_principal(raw: Any) -> ConfiguredPrincipal:
        if not isinstance(raw, dict):
            raise InvalidIdentityConfiguration("Each Principal must be a JSON object")
        try:
            principal_id = str(raw["principal_id"]).strip()
            tenant_id = str(raw["tenant_id"]).strip()
            principal_type = PrincipalType(raw.get("principal_type", "USER"))
            status = PrincipalStatus(raw.get("status", "ACTIVE"))
            roles_raw = raw["roles"]
            token_sha256 = str(raw["token_sha256"]).strip().lower()
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidIdentityConfiguration("Principal configuration is invalid") from exc
        if not principal_id or not tenant_id:
            raise InvalidIdentityConfiguration("Principal ID and tenant ID must not be blank")
        if not isinstance(roles_raw, list) or not roles_raw:
            raise InvalidIdentityConfiguration("Principal roles must be a non-empty array")
        try:
            roles = frozenset(Role(value) for value in roles_raw)
        except ValueError as exc:
            raise InvalidIdentityConfiguration("Principal contains an unknown role") from exc
        if len(token_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in token_sha256
        ):
            raise InvalidIdentityConfiguration("token_sha256 must be a 64-character hex digest")
        expires_at = None
        if raw.get("expires_at") is not None:
            try:
                expires_at = datetime.fromisoformat(str(raw["expires_at"]).replace("Z", "+00:00"))
            except ValueError as exc:
                raise InvalidIdentityConfiguration("expires_at must be ISO-8601") from exc
            if expires_at.tzinfo is None:
                raise InvalidIdentityConfiguration("expires_at must include a timezone")
            expires_at = expires_at.astimezone(timezone.utc)
        return ConfiguredPrincipal(
            principal_id=principal_id,
            tenant_id=tenant_id,
            principal_type=principal_type,
            status=status,
            roles=roles,
            token_sha256=token_sha256,
            expires_at=expires_at,
        )


class IdentityAdministrationService:
    def __init__(self, *, uow_factory: UnitOfWorkFactory, tenant_id: str) -> None:
        self._uow_factory = uow_factory
        self._tenant_id = tenant_id

    def bootstrap(self, configured: tuple[ConfiguredPrincipal, ...]) -> None:
        with self._uow_factory() as uow:
            changed = False
            for item in configured:
                try:
                    principal_id = UUID(item.principal_id)
                except ValueError as exc:
                    raise InvalidIdentityConfiguration(
                        "Persistent configured Principal IDs must be UUIDs"
                    ) from exc
                if item.tenant_id != self._tenant_id:
                    raise InvalidIdentityConfiguration("Bootstrap Principal tenant mismatch")
                principal = uow.identity.get_principal(principal_id)
                if principal is None:
                    principal = Principal.create(
                        principal_id=principal_id,
                        tenant_id=item.tenant_id,
                        principal_type=item.principal_type,
                        display_name=item.principal_id,
                    )
                    uow.identity.add_principal(principal)
                    for role in item.roles:
                        uow.identity.add_role_binding(
                            RoleBinding.create(
                                tenant_id=self._tenant_id,
                                principal_id=principal_id,
                                role=role,
                                created_by="bootstrap",
                            )
                        )
                    changed = True
            if changed:
                uow.commit()

    def create_principal(
        self,
        *,
        principal_type: PrincipalType,
        display_name: str,
        actor: str,
        idempotency_key: str,
    ) -> Principal:
        request = {"principal_type": principal_type.value, "display_name": display_name.strip()}
        request_hash = _request_hash(request)
        scope = f"identity-principal:{self._tenant_id}:{actor}"
        with self._uow_factory() as uow:
            replay = self._replay(uow, scope, idempotency_key, request_hash)
            if replay is not None:
                principal = uow.identity.get_principal(UUID(replay["principal_id"]))
                if principal is None:
                    raise IdentityConflict("Principal idempotency result was lost")
                return principal
            principal = Principal.create(
                principal_id=None,
                tenant_id=self._tenant_id,
                principal_type=principal_type,
                display_name=display_name,
            )
            uow.identity.add_principal(principal)
            self._record(
                uow, scope, idempotency_key, request_hash, {"principal_id": str(principal.id)}
            )
            self._event(uow, principal.id, "agentmesh.identity.principal-created", actor)
            uow.commit()
            return principal

    def list_principals(self, *, limit: int, offset: int) -> list[Principal]:
        with self._uow_factory() as uow:
            return uow.identity.list_principals(
                tenant_id=self._tenant_id, limit=limit, offset=offset
            )

    def change_status(
        self, principal_id: UUID, *, status: PrincipalStatus, actor: str
    ) -> Principal:
        with self._uow_factory() as uow:
            principal = uow.identity.get_principal(principal_id, for_update=True)
            self._require_principal(principal)
            assert principal is not None
            if principal.status is status:
                return principal
            updated = principal.change_status(status)
            uow.identity.save_principal(updated)
            self._event(uow, updated.id, "agentmesh.identity.principal-status-changed", actor)
            uow.commit()
            return updated

    def add_external_identity(
        self,
        principal_id: UUID,
        *,
        issuer: str,
        subject: str,
        actor: str,
        idempotency_key: str,
    ) -> ExternalIdentity:
        normalized_issuer = issuer.strip().rstrip("/")
        normalized_subject = subject.strip()
        request_hash = _request_hash(
            {
                "principal_id": str(principal_id),
                "issuer": normalized_issuer,
                "subject": normalized_subject,
            }
        )
        scope = f"identity-external:{self._tenant_id}:{actor}"
        with self._uow_factory() as uow:
            principal = uow.identity.get_principal(principal_id)
            self._require_principal(principal)
            replay = self._replay(uow, scope, idempotency_key, request_hash)
            existing = uow.identity.get_external_identity(
                tenant_id=self._tenant_id,
                issuer=normalized_issuer,
                subject=normalized_subject,
            )
            if replay is not None:
                if existing is None or str(existing.id) != replay["external_identity_id"]:
                    raise IdentityConflict("External identity idempotency result was lost")
                return existing
            if existing is not None:
                if existing.principal_id == principal_id:
                    self._record(
                        uow,
                        scope,
                        idempotency_key,
                        request_hash,
                        {"external_identity_id": str(existing.id)},
                    )
                    uow.commit()
                    return existing
                raise IdentityConflict("External identity is already mapped")
            identity = ExternalIdentity.create(
                tenant_id=self._tenant_id,
                principal_id=principal_id,
                issuer=issuer,
                subject=subject,
                created_by=actor,
            )
            uow.identity.add_external_identity(identity)
            self._record(
                uow,
                scope,
                idempotency_key,
                request_hash,
                {"external_identity_id": str(identity.id)},
            )
            self._event(uow, principal_id, "agentmesh.identity.external-identity-added", actor)
            uow.commit()
            return identity

    def grant_role(
        self,
        principal_id: UUID,
        *,
        role: Role,
        actor: str,
        effective_at: datetime | None,
        expires_at: datetime | None,
        idempotency_key: str,
    ) -> RoleBinding:
        request = {
            "principal_id": str(principal_id),
            "role": role.value,
            "effective_at": effective_at.isoformat() if effective_at else None,
            "expires_at": expires_at.isoformat() if expires_at else None,
        }
        request_hash = _request_hash(request)
        scope = f"identity-role:{self._tenant_id}:{actor}"
        with self._uow_factory() as uow:
            principal = uow.identity.get_principal(principal_id)
            self._require_principal(principal)
            replay = self._replay(uow, scope, idempotency_key, request_hash)
            if replay is not None:
                binding = uow.identity.get_role_binding(UUID(replay["binding_id"]))
                if binding is None:
                    raise IdentityConflict("RoleBinding idempotency result was lost")
                return binding
            binding = RoleBinding.create(
                tenant_id=self._tenant_id,
                principal_id=principal_id,
                role=role,
                created_by=actor,
                effective_at=effective_at,
                expires_at=expires_at,
            )
            uow.identity.add_role_binding(binding)
            self._record(uow, scope, idempotency_key, request_hash, {"binding_id": str(binding.id)})
            self._event(uow, principal_id, "agentmesh.identity.role-granted", actor)
            uow.commit()
            return binding

    def list_role_bindings(self, principal_id: UUID) -> list[RoleBinding]:
        with self._uow_factory() as uow:
            principal = uow.identity.get_principal(principal_id)
            self._require_principal(principal)
            return uow.identity.list_role_bindings(principal_id)

    def revoke_role(self, binding_id: UUID, *, actor: str, reason: str) -> RoleBinding:
        with self._uow_factory() as uow:
            binding = uow.identity.get_role_binding(binding_id, for_update=True)
            if binding is None or binding.tenant_id != self._tenant_id:
                raise RoleBindingNotFound(f"RoleBinding {binding_id} was not found")
            updated = binding.revoke(actor=actor, reason=reason)
            if updated is binding:
                return binding
            uow.identity.save_role_binding(updated)
            self._event(uow, updated.principal_id, "agentmesh.identity.role-revoked", actor)
            uow.commit()
            return updated

    def _require_principal(self, principal: Principal | None) -> None:
        if principal is None or principal.tenant_id != self._tenant_id:
            raise PrincipalNotFound("Principal was not found")

    @staticmethod
    def _replay(uow, scope: str, key: str, request_hash: str) -> dict[str, Any] | None:
        normalized = key.strip()
        if not normalized:
            raise InvalidIdentity("Idempotency-Key must not be blank")
        uow.idempotency.lock(scope, normalized)
        existing = uow.idempotency.get(scope, normalized)
        if existing is None:
            return None
        if existing.request_hash != request_hash:
            raise IdempotencyConflict("Idempotency-Key was reused for a different identity request")
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

    def _event(self, uow, aggregate_id: UUID, schema: str, actor: str) -> None:
        uow.outbox.add(
            MessageEnvelope.domain_event(
                schema_name=schema,
                tenant_id=self._tenant_id,
                aggregate_id=aggregate_id,
                payload={"principal_id": str(aggregate_id), "actor": actor},
            )
        )


def _request_hash(value: dict[str, Any]) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
