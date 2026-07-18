from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from agentmesh.domain.errors import (
    AuthenticationFailed,
    AuthenticationRequired,
    AuthorizationDenied,
    InvalidIdentityConfiguration,
)
from agentmesh.domain.identity import (
    Permission,
    PrincipalContext,
    PrincipalStatus,
    PrincipalType,
    Role,
)


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
    ) -> None:
        self.enabled = enabled
        self.tenant_id = tenant_id
        self._principals = self._parse_principals(principals_json) if enabled else ()
        if enabled and not self._principals:
            raise InvalidIdentityConfiguration(
                "identity_rbac requires at least one configured Principal"
            )

    def authenticate(self, authorization: str | None) -> PrincipalContext:
        if not self.enabled:
            return self.local_context()
        if authorization is None:
            raise AuthenticationRequired("Bearer authentication is required")
        scheme, separator, token = authorization.partition(" ")
        if separator != " " or scheme.lower() != "bearer" or not token or " " in token:
            raise AuthenticationFailed("Invalid bearer credential")
        if len(token.encode("utf-8")) < 32:
            raise AuthenticationFailed("Invalid bearer credential")

        candidate_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        matched = None
        for principal in self._principals:
            if hmac.compare_digest(principal.token_sha256, candidate_digest):
                matched = principal
        if matched is None:
            raise AuthenticationFailed("Invalid bearer credential")
        now = datetime.now(timezone.utc)
        if matched.status is not PrincipalStatus.ACTIVE:
            raise AuthenticationFailed("Principal is not active")
        if matched.expires_at is not None and matched.expires_at <= now:
            raise AuthenticationFailed("Bearer credential has expired")
        if matched.tenant_id != self.tenant_id:
            raise AuthenticationFailed("Principal tenant does not match this control plane")
        return PrincipalContext(
            principal_id=matched.principal_id,
            tenant_id=matched.tenant_id,
            principal_type=matched.principal_type,
            roles=matched.roles,
            authenticated=True,
            authentication_method="bearer_sha256",
        )

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
