from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from uuid import UUID, uuid4

from agentmesh.domain.errors import InvalidIdentity


class PrincipalType(str, Enum):
    USER = "USER"
    SERVICE = "SERVICE"
    AGENT = "AGENT"
    EXTERNAL_PEER = "EXTERNAL_PEER"


class PrincipalStatus(str, Enum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    DEACTIVATED = "DEACTIVATED"


class Role(str, Enum):
    TENANT_ADMIN = "TENANT_ADMIN"
    OPERATOR = "OPERATOR"
    AGENT_AUTHOR = "AGENT_AUTHOR"
    AGENT_PUBLISHER = "AGENT_PUBLISHER"
    APPROVER = "APPROVER"
    AUDITOR = "AUDITOR"


class Permission(str, Enum):
    SYSTEM_INSPECT = "system:inspect"
    TASK_READ = "task:read"
    TASK_CREATE = "task:create"
    TASK_OPERATE = "task:operate"
    TASK_RESOLVE = "task:resolve"
    AGENT_READ = "agent:read"
    AGENT_MANAGE = "agent:manage"
    AGENT_PUBLISH = "agent:publish"
    ARTIFACT_READ = "artifact:read"
    ARTIFACT_WRITE = "artifact:write"
    TOOL_AUDIT_READ = "tool-audit:read"
    OBSERVABILITY_READ = "observability:read"
    POLICY_REQUEST = "policy:request"
    APPROVAL_READ = "approval:read"
    APPROVAL_DECIDE = "approval:decide"
    IDENTITY_ADMIN = "identity:admin"
    IDENTITY_READ = "identity:read"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.TENANT_ADMIN: frozenset(Permission),
    Role.OPERATOR: frozenset(
        {
            Permission.SYSTEM_INSPECT,
            Permission.TASK_READ,
            Permission.TASK_CREATE,
            Permission.TASK_OPERATE,
            Permission.TASK_RESOLVE,
            Permission.AGENT_READ,
            Permission.ARTIFACT_READ,
            Permission.ARTIFACT_WRITE,
            Permission.TOOL_AUDIT_READ,
            Permission.OBSERVABILITY_READ,
            Permission.POLICY_REQUEST,
        }
    ),
    Role.AGENT_AUTHOR: frozenset(
        {
            Permission.SYSTEM_INSPECT,
            Permission.AGENT_READ,
            Permission.AGENT_MANAGE,
        }
    ),
    Role.AGENT_PUBLISHER: frozenset(
        {
            Permission.SYSTEM_INSPECT,
            Permission.AGENT_READ,
            Permission.AGENT_MANAGE,
            Permission.AGENT_PUBLISH,
            Permission.POLICY_REQUEST,
        }
    ),
    Role.APPROVER: frozenset(
        {
            Permission.SYSTEM_INSPECT,
            Permission.TASK_READ,
            Permission.AGENT_READ,
            Permission.APPROVAL_READ,
            Permission.APPROVAL_DECIDE,
        }
    ),
    Role.AUDITOR: frozenset(
        {
            Permission.SYSTEM_INSPECT,
            Permission.TASK_READ,
            Permission.AGENT_READ,
            Permission.ARTIFACT_READ,
            Permission.TOOL_AUDIT_READ,
            Permission.OBSERVABILITY_READ,
            Permission.APPROVAL_READ,
            Permission.IDENTITY_READ,
        }
    ),
}


class RoleBindingStatus(str, Enum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Principal:
    id: UUID
    tenant_id: str
    principal_type: PrincipalType
    status: PrincipalStatus
    display_name: str
    created_at: datetime
    updated_at: datetime
    revision: int = 1

    @classmethod
    def create(
        cls,
        *,
        principal_id: UUID | None,
        tenant_id: str,
        principal_type: PrincipalType,
        display_name: str,
        now: datetime | None = None,
    ) -> Principal:
        tenant = tenant_id.strip()
        name = display_name.strip()
        if not tenant or not name or len(name) > 200:
            raise InvalidIdentity("Principal tenant and display name must be valid")
        timestamp = now or _utc_now()
        return cls(
            id=principal_id or uuid4(),
            tenant_id=tenant,
            principal_type=principal_type,
            status=PrincipalStatus.ACTIVE,
            display_name=name,
            created_at=timestamp,
            updated_at=timestamp,
        )

    def change_status(self, status: PrincipalStatus, *, now: datetime | None = None) -> Principal:
        if self.status is PrincipalStatus.DEACTIVATED and status is not PrincipalStatus.DEACTIVATED:
            raise InvalidIdentity("A deactivated Principal cannot be reactivated")
        return replace(
            self,
            status=status,
            updated_at=now or _utc_now(),
            revision=self.revision + 1,
        )


@dataclass(frozen=True)
class ExternalIdentity:
    id: UUID
    tenant_id: str
    principal_id: UUID
    issuer: str
    subject: str
    created_at: datetime
    created_by: str

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        principal_id: UUID,
        issuer: str,
        subject: str,
        created_by: str,
        now: datetime | None = None,
    ) -> ExternalIdentity:
        normalized_issuer = issuer.strip().rstrip("/")
        normalized_subject = subject.strip()
        if not tenant_id.strip() or not normalized_issuer or not normalized_subject:
            raise InvalidIdentity("External identity fields must not be blank")
        if not normalized_issuer.startswith("https://"):
            raise InvalidIdentity("External identity issuer must use HTTPS")
        return cls(
            id=uuid4(),
            tenant_id=tenant_id.strip(),
            principal_id=principal_id,
            issuer=normalized_issuer,
            subject=normalized_subject,
            created_at=now or _utc_now(),
            created_by=created_by.strip(),
        )


@dataclass(frozen=True)
class RoleBinding:
    id: UUID
    tenant_id: str
    principal_id: UUID
    role: Role
    status: RoleBindingStatus
    effective_at: datetime
    expires_at: datetime | None
    created_at: datetime
    created_by: str
    revoked_at: datetime | None = None
    revoked_by: str | None = None
    revoke_reason: str | None = None
    revision: int = 1

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        principal_id: UUID,
        role: Role,
        created_by: str,
        effective_at: datetime | None = None,
        expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> RoleBinding:
        timestamp = now or _utc_now()
        effective = effective_at or timestamp
        if effective.tzinfo is None or (expires_at is not None and expires_at.tzinfo is None):
            raise InvalidIdentity("RoleBinding timestamps must be timezone-aware")
        if expires_at is not None and expires_at <= effective:
            raise InvalidIdentity("RoleBinding expiry must be after its effective time")
        if not tenant_id.strip() or not created_by.strip():
            raise InvalidIdentity("RoleBinding tenant and creator must not be blank")
        return cls(
            id=uuid4(),
            tenant_id=tenant_id.strip(),
            principal_id=principal_id,
            role=role,
            status=RoleBindingStatus.ACTIVE,
            effective_at=effective,
            expires_at=expires_at,
            created_at=timestamp,
            created_by=created_by.strip(),
        )

    def is_effective(self, now: datetime) -> bool:
        return (
            self.status is RoleBindingStatus.ACTIVE
            and self.effective_at <= now
            and (self.expires_at is None or self.expires_at > now)
        )

    def revoke(self, *, actor: str, reason: str, now: datetime | None = None) -> RoleBinding:
        if self.status is RoleBindingStatus.REVOKED:
            return self
        if not actor.strip() or not reason.strip():
            raise InvalidIdentity("RoleBinding revocation requires actor and reason")
        timestamp = now or _utc_now()
        return replace(
            self,
            status=RoleBindingStatus.REVOKED,
            revoked_at=timestamp,
            revoked_by=actor.strip(),
            revoke_reason=reason.strip(),
            revision=self.revision + 1,
        )


@dataclass(frozen=True)
class PrincipalContext:
    principal_id: str
    tenant_id: str
    principal_type: PrincipalType
    roles: frozenset[Role]
    authenticated: bool
    authentication_method: str

    @property
    def permissions(self) -> frozenset[Permission]:
        return frozenset(
            permission
            for role in self.roles
            for permission in ROLE_PERMISSIONS.get(role, frozenset())
        )

    def audit_actor(self, supplied_actor: str) -> str:
        return self.principal_id if self.authenticated else supplied_actor
