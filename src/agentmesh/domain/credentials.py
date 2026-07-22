from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from agentmesh.domain.errors import InvalidCredential
from agentmesh.domain.tasks import utc_now

ENVIRONMENT_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,254}$")


class SecretProvider(str, Enum):
    ENVIRONMENT = "ENVIRONMENT"


class SecretPurpose(str, Enum):
    A2A_HTTP_BEARER = "A2A_HTTP_BEARER"
    MCP_HTTP_BEARER = "MCP_HTTP_BEARER"
    MODEL_PROVIDER_API_KEY = "MODEL_PROVIDER_API_KEY"


class SecretReferenceStatus(str, Enum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class CredentialBindingStatus(str, Enum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class CredentialLeaseStatus(str, Enum):
    REQUESTED = "REQUESTED"
    ISSUED = "ISSUED"
    USED = "USED"
    FAILED = "FAILED"


TERMINAL_LEASE_STATUSES = {CredentialLeaseStatus.USED, CredentialLeaseStatus.FAILED}


@dataclass(frozen=True)
class SecretReference:
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
    revision: int = 1

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        provider: SecretProvider,
        external_key: str,
        version_selector: str | None,
        purpose: SecretPurpose,
        allowed_audiences: tuple[str, ...],
        created_by: str,
    ) -> SecretReference:
        tenant = tenant_id.strip()
        actor = created_by.strip()
        key = external_key.strip()
        if not tenant or not actor:
            raise InvalidCredential("SecretReference tenant and creator are required")
        if provider is SecretProvider.ENVIRONMENT and not ENVIRONMENT_KEY.fullmatch(key):
            raise InvalidCredential("Environment SecretReference key is invalid")
        selector = version_selector.strip() if version_selector else None
        if selector is not None and len(selector) > 128:
            raise InvalidCredential("SecretReference version selector is too long")
        audiences = _audiences(allowed_audiences)
        now = utc_now()
        return cls(
            id=uuid4(),
            tenant_id=tenant,
            provider=provider,
            external_key=key,
            version_selector=selector,
            purpose=purpose,
            allowed_audiences=audiences,
            status=SecretReferenceStatus.ACTIVE,
            created_by=actor,
            created_at=now,
            updated_at=now,
        )

    def revoke(self) -> SecretReference:
        if self.status is SecretReferenceStatus.REVOKED:
            return self
        return replace(
            self,
            status=SecretReferenceStatus.REVOKED,
            updated_at=utc_now(),
            revision=self.revision + 1,
        )


@dataclass(frozen=True)
class CredentialBinding:
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
    revision: int = 1

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        workload_principal_id: UUID,
        peer_id: UUID,
        card_snapshot_id: UUID,
        card_digest: str,
        secret_reference_id: UUID,
        scheme_name: str,
        auth_scheme: str,
        audience: str,
        scopes: tuple[str, ...],
        environment: str,
        expires_at: datetime,
        created_by: str,
    ) -> CredentialBinding:
        tenant = tenant_id.strip()
        scheme = scheme_name.strip()
        auth = auth_scheme.strip()
        deployment_environment = environment.strip().lower()
        actor = created_by.strip()
        now = utc_now()
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise InvalidCredential("CredentialBinding expiry must include a UTC offset")
        if not tenant or not actor or not scheme or len(scheme) > 128:
            raise InvalidCredential("CredentialBinding identity fields are invalid")
        if auth.lower() != "bearer":
            raise InvalidCredential("This baseline supports only HTTP Bearer credentials")
        if not card_digest.startswith("sha256:"):
            raise InvalidCredential("CredentialBinding Card digest is invalid")
        if not deployment_environment or len(deployment_environment) > 64:
            raise InvalidCredential("CredentialBinding environment is invalid")
        if expires_at <= now or expires_at > now + timedelta(days=365):
            raise InvalidCredential("CredentialBinding expiry must be within one year")
        return cls(
            id=uuid4(),
            tenant_id=tenant,
            workload_principal_id=workload_principal_id,
            peer_id=peer_id,
            card_snapshot_id=card_snapshot_id,
            card_digest=card_digest,
            secret_reference_id=secret_reference_id,
            scheme_name=scheme,
            auth_scheme="Bearer",
            audience=_audience(audience),
            scopes=_scopes(scopes),
            environment=deployment_environment,
            expires_at=expires_at,
            status=CredentialBindingStatus.ACTIVE,
            created_by=actor,
            created_at=now,
            updated_at=now,
        )

    def revoke(self) -> CredentialBinding:
        if self.status is CredentialBindingStatus.REVOKED:
            return self
        return replace(
            self,
            status=CredentialBindingStatus.REVOKED,
            updated_at=utc_now(),
            revision=self.revision + 1,
        )


@dataclass(frozen=True)
class CredentialLease:
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
    revision: int = 1

    @classmethod
    def request(
        cls,
        *,
        tenant_id: str,
        binding: CredentialBinding,
        task_id: UUID,
        run_id: UUID,
        ttl_seconds: int,
    ) -> CredentialLease:
        if ttl_seconds < 1 or ttl_seconds > 300:
            raise InvalidCredential("CredentialLease TTL must be between 1 and 300 seconds")
        now = utc_now()
        expires_at = min(now + timedelta(seconds=ttl_seconds), binding.expires_at)
        return cls(
            id=uuid4(),
            tenant_id=tenant_id,
            binding_id=binding.id,
            secret_reference_id=binding.secret_reference_id,
            workload_principal_id=binding.workload_principal_id,
            peer_id=binding.peer_id,
            card_snapshot_id=binding.card_snapshot_id,
            task_id=task_id,
            run_id=run_id,
            audience=binding.audience,
            scopes=binding.scopes,
            status=CredentialLeaseStatus.REQUESTED,
            issued_at=None,
            expires_at=expires_at,
            completed_at=None,
            error=None,
            created_at=now,
            updated_at=now,
        )

    def issue(self) -> CredentialLease:
        self._require(CredentialLeaseStatus.REQUESTED, "issue")
        now = utc_now()
        if now >= self.expires_at:
            raise InvalidCredential("CredentialLease expired before issuance")
        return replace(
            self,
            status=CredentialLeaseStatus.ISSUED,
            issued_at=now,
            updated_at=now,
            revision=self.revision + 1,
        )

    def settle(self, *, used: bool, error: str | None = None) -> CredentialLease:
        self._require(CredentialLeaseStatus.ISSUED, "settle")
        now = utc_now()
        normalized_error = error.strip()[:1000] if error else None
        if not used and not normalized_error:
            raise InvalidCredential("Failed CredentialLease requires an error category")
        return replace(
            self,
            status=CredentialLeaseStatus.USED if used else CredentialLeaseStatus.FAILED,
            completed_at=now,
            error=None if used else normalized_error,
            updated_at=now,
            revision=self.revision + 1,
        )

    def fail_request(self, error: str) -> CredentialLease:
        self._require(CredentialLeaseStatus.REQUESTED, "fail")
        normalized = error.strip()[:1000]
        if not normalized:
            raise InvalidCredential("Failed CredentialLease requires an error category")
        now = utc_now()
        return replace(
            self,
            status=CredentialLeaseStatus.FAILED,
            completed_at=now,
            error=normalized,
            updated_at=now,
            revision=self.revision + 1,
        )

    def _require(self, expected: CredentialLeaseStatus, action: str) -> None:
        if self.status is not expected:
            raise InvalidCredential(
                f"Cannot {action} CredentialLease {self.id} from {self.status.value}"
            )


@dataclass(frozen=True, repr=False)
class CredentialMaterial:
    lease_id: UUID
    auth_scheme: str
    value: str

    def __repr__(self) -> str:
        return (
            f"CredentialMaterial(lease_id={self.lease_id!r}, "
            f"auth_scheme={self.auth_scheme!r}, value=<redacted>)"
        )


@dataclass(frozen=True, repr=False)
class CredentialGrant:
    lease: CredentialLease
    material: CredentialMaterial

    def __repr__(self) -> str:
        return f"CredentialGrant(lease={self.lease!r}, material=<redacted>)"


@dataclass(frozen=True)
class McpCredentialBinding:
    id: UUID
    tenant_id: str
    workload_principal_id: UUID
    server_id: UUID
    server_version_id: UUID
    configuration_digest: str
    secret_reference_id: UUID
    auth_scheme: str
    audience: str
    scopes: tuple[str, ...]
    environment: str
    expires_at: datetime
    status: CredentialBindingStatus
    created_by: str
    created_at: datetime
    updated_at: datetime
    revision: int = 1

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        workload_principal_id: UUID,
        server_id: UUID,
        server_version_id: UUID,
        configuration_digest: str,
        secret_reference_id: UUID,
        auth_scheme: str,
        audience: str,
        scopes: tuple[str, ...],
        environment: str,
        expires_at: datetime,
        created_by: str,
    ) -> McpCredentialBinding:
        tenant = tenant_id.strip()
        actor = created_by.strip()
        auth = auth_scheme.strip()
        deployment_environment = environment.strip().lower()
        now = utc_now()
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise InvalidCredential("MCP CredentialBinding expiry must include a UTC offset")
        if not tenant or not actor:
            raise InvalidCredential("MCP CredentialBinding identity fields are invalid")
        if auth.lower() != "bearer":
            raise InvalidCredential("This baseline supports only HTTP Bearer credentials")
        if not configuration_digest.startswith("sha256:"):
            raise InvalidCredential("MCP CredentialBinding configuration digest is invalid")
        if not deployment_environment or len(deployment_environment) > 64:
            raise InvalidCredential("MCP CredentialBinding environment is invalid")
        if expires_at <= now or expires_at > now + timedelta(days=365):
            raise InvalidCredential("MCP CredentialBinding expiry must be within one year")
        return cls(
            id=uuid4(),
            tenant_id=tenant,
            workload_principal_id=workload_principal_id,
            server_id=server_id,
            server_version_id=server_version_id,
            configuration_digest=configuration_digest,
            secret_reference_id=secret_reference_id,
            auth_scheme="Bearer",
            audience=_audience(audience),
            scopes=_scopes(scopes),
            environment=deployment_environment,
            expires_at=expires_at,
            status=CredentialBindingStatus.ACTIVE,
            created_by=actor,
            created_at=now,
            updated_at=now,
        )

    def revoke(self) -> McpCredentialBinding:
        if self.status is CredentialBindingStatus.REVOKED:
            return self
        return replace(
            self,
            status=CredentialBindingStatus.REVOKED,
            updated_at=utc_now(),
            revision=self.revision + 1,
        )


@dataclass(frozen=True)
class McpCredentialLease:
    id: UUID
    tenant_id: str
    binding_id: UUID
    secret_reference_id: UUID
    workload_principal_id: UUID
    server_id: UUID
    server_version_id: UUID
    tool_invocation_id: UUID
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
    revision: int = 1

    @classmethod
    def request(
        cls,
        *,
        tenant_id: str,
        binding: McpCredentialBinding,
        tool_invocation_id: UUID,
        task_id: UUID,
        run_id: UUID,
        ttl_seconds: int,
    ) -> McpCredentialLease:
        if ttl_seconds < 1 or ttl_seconds > 300:
            raise InvalidCredential("MCP CredentialLease TTL must be between 1 and 300 seconds")
        now = utc_now()
        return cls(
            id=uuid4(),
            tenant_id=tenant_id,
            binding_id=binding.id,
            secret_reference_id=binding.secret_reference_id,
            workload_principal_id=binding.workload_principal_id,
            server_id=binding.server_id,
            server_version_id=binding.server_version_id,
            tool_invocation_id=tool_invocation_id,
            task_id=task_id,
            run_id=run_id,
            audience=binding.audience,
            scopes=binding.scopes,
            status=CredentialLeaseStatus.REQUESTED,
            issued_at=None,
            expires_at=now + timedelta(seconds=ttl_seconds),
            completed_at=None,
            error=None,
            created_at=now,
            updated_at=now,
        )

    def issue(self) -> McpCredentialLease:
        self._require(CredentialLeaseStatus.REQUESTED, "issue")
        now = utc_now()
        if now >= self.expires_at:
            raise InvalidCredential("MCP CredentialLease expired before issuance")
        return replace(
            self,
            status=CredentialLeaseStatus.ISSUED,
            issued_at=now,
            updated_at=now,
            revision=self.revision + 1,
        )

    def settle(self, *, used: bool, error: str | None = None) -> McpCredentialLease:
        self._require(CredentialLeaseStatus.ISSUED, "settle")
        normalized_error = error.strip()[:1000] if error else None
        if not used and not normalized_error:
            raise InvalidCredential("Failed MCP CredentialLease requires an error category")
        now = utc_now()
        return replace(
            self,
            status=CredentialLeaseStatus.USED if used else CredentialLeaseStatus.FAILED,
            completed_at=now,
            error=None if used else normalized_error,
            updated_at=now,
            revision=self.revision + 1,
        )

    def fail_request(self, error: str) -> McpCredentialLease:
        self._require(CredentialLeaseStatus.REQUESTED, "fail")
        normalized = error.strip()[:1000]
        if not normalized:
            raise InvalidCredential("Failed MCP CredentialLease requires an error category")
        now = utc_now()
        return replace(
            self,
            status=CredentialLeaseStatus.FAILED,
            completed_at=now,
            error=normalized,
            updated_at=now,
            revision=self.revision + 1,
        )

    def _require(self, expected: CredentialLeaseStatus, action: str) -> None:
        if self.status is not expected:
            raise InvalidCredential(
                f"Cannot {action} MCP CredentialLease {self.id} from {self.status.value}"
            )


@dataclass(frozen=True, repr=False)
class McpCredentialGrant:
    lease: McpCredentialLease
    material: CredentialMaterial

    def __repr__(self) -> str:
        return f"McpCredentialGrant(lease={self.lease!r}, material=<redacted>)"


def _audiences(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(dict.fromkeys(_audience(value) for value in values))
    if not normalized or len(normalized) > 32:
        raise InvalidCredential("SecretReference must allow 1-32 audiences")
    return normalized


def _audience(value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or len(normalized) > 2048
    ):
        raise InvalidCredential("Credential audience must be a bounded HTTPS URL")
    return normalized


def _scopes(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(sorted(dict.fromkeys(value.strip() for value in values)))
    if len(normalized) > 64 or any(not value or len(value) > 256 for value in normalized):
        raise InvalidCredential("Credential scopes are invalid")
    return normalized
