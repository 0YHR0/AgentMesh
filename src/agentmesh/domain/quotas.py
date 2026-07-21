from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from agentmesh.domain.errors import InvalidTaskInput
from agentmesh.domain.tasks import utc_now


class QuotaScope(str, Enum):
    TENANT = "TENANT"
    PROJECT = "PROJECT"


@dataclass(frozen=True)
class QuotaPolicy:
    id: UUID
    tenant_id: str
    scope: QuotaScope
    project_id: str | None
    max_concurrent_attempts: int
    weight: int
    version: int
    active: bool
    created_by: str
    created_at: datetime

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        scope: QuotaScope,
        project_id: str | None,
        max_concurrent_attempts: int,
        weight: int,
        version: int,
        created_by: str,
    ) -> QuotaPolicy:
        tenant = tenant_id.strip()
        project = project_id.strip() if project_id is not None else None
        actor = created_by.strip()
        if not tenant or not actor:
            raise InvalidTaskInput("Quota policy tenant and creator must not be empty")
        if scope is QuotaScope.TENANT and project is not None:
            raise InvalidTaskInput("Tenant quota policy must not specify a project")
        if scope is QuotaScope.PROJECT and not project:
            raise InvalidTaskInput("Project quota policy requires a project ID")
        if project is not None and len(project) > 128:
            raise InvalidTaskInput("Quota project ID must not exceed 128 characters")
        if isinstance(max_concurrent_attempts, bool) or not 1 <= max_concurrent_attempts <= 100_000:
            raise InvalidTaskInput("Quota concurrency must be between 1 and 100000")
        if isinstance(weight, bool) or not 1 <= weight <= 1_000:
            raise InvalidTaskInput("Quota scheduling weight must be between 1 and 1000")
        if version < 1:
            raise InvalidTaskInput("Quota policy version must be positive")
        return cls(
            id=uuid4(),
            tenant_id=tenant,
            scope=scope,
            project_id=project,
            max_concurrent_attempts=max_concurrent_attempts,
            weight=weight,
            version=version,
            active=True,
            created_by=actor,
            created_at=utc_now(),
        )

    @property
    def scope_key(self) -> str:
        return self.project_id or self.tenant_id


@dataclass
class QuotaReservation:
    id: UUID
    policy_id: UUID
    attempt_id: UUID
    tenant_id: str
    project_id: str
    acquired_at: datetime
    released_at: datetime | None

    @classmethod
    def acquire(
        cls, *, policy_id: UUID, attempt_id: UUID, tenant_id: str, project_id: str
    ) -> QuotaReservation:
        return cls(
            id=uuid4(),
            policy_id=policy_id,
            attempt_id=attempt_id,
            tenant_id=tenant_id,
            project_id=project_id,
            acquired_at=utc_now(),
            released_at=None,
        )

    def release(self) -> None:
        if self.released_at is None:
            self.released_at = utc_now()


@dataclass(frozen=True)
class QuotaPolicyStatus:
    policy: QuotaPolicy
    active_reservations: int
