from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PrincipalType(str, Enum):
    USER = "USER"
    SERVICE = "SERVICE"


class PrincipalStatus(str, Enum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    DEACTIVATED = "DEACTIVATED"


class Role(str, Enum):
    TENANT_ADMIN = "TENANT_ADMIN"
    OPERATOR = "OPERATOR"
    AGENT_AUTHOR = "AGENT_AUTHOR"
    AGENT_PUBLISHER = "AGENT_PUBLISHER"
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
        }
    ),
}


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
