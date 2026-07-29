from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
from uuid import UUID, uuid4

from agentmesh.domain.errors import InvalidOrganizationalMemory
from agentmesh.domain.tasks import utc_now


class MemoryNamespaceType(str, Enum):
    COMPANY = "COMPANY"
    UNIT = "UNIT"
    PROJECT = "PROJECT"
    POSITION = "POSITION"
    EMPLOYEE = "EMPLOYEE"
    RELATIONSHIP = "RELATIONSHIP"
    USER = "USER"


class MemoryType(str, Enum):
    FACT = "FACT"
    PREFERENCE = "PREFERENCE"
    DECISION = "DECISION"
    PATTERN = "PATTERN"
    PROCEDURE = "PROCEDURE"
    FEEDBACK = "FEEDBACK"
    RELATIONSHIP = "RELATIONSHIP"


class MemorySensitivity(str, Enum):
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"
    RESTRICTED = "RESTRICTED"


class MemoryStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    ACCEPTED = "ACCEPTED"
    SUPERSEDED = "SUPERSEDED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    REJECTED = "REJECTED"


class MemoryProvenanceType(str, Enum):
    TASK = "TASK"
    RUN = "RUN"
    ATTEMPT = "ATTEMPT"
    ARTIFACT_VERSION = "ARTIFACT_VERSION"
    USER_STATEMENT = "USER_STATEMENT"
    BUSINESS_OBJECT_REVISION = "BUSINESS_OBJECT_REVISION"
    RESOURCE_SNAPSHOT = "RESOURCE_SNAPSHOT"
    IMPORTED_POLICY = "IMPORTED_POLICY"


SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.I),
    re.compile(r"\b(?:sk|ghp|github_pat)_[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:password|passwd|secret|api[_-]?key)\s*[:=]\s*\S+", re.I),
)


def _required(value: str, label: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise InvalidOrganizationalMemory(f"{label} is required")
    if len(normalized) > maximum:
        raise InvalidOrganizationalMemory(
            f"{label} must not exceed {maximum} characters"
        )
    return normalized


def _digest(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def namespace_key(namespace_type: MemoryNamespaceType, namespace_id: str) -> str:
    normalized = _required(namespace_id, "Memory namespace ID", 255)
    return f"{namespace_type.value.lower()}/{normalized}"


@dataclass
class MemoryPolicy:
    id: UUID
    company_id: UUID
    key: str
    version: int
    readable_namespace_patterns: list[str]
    writable_namespace_patterns: list[str]
    allowed_memory_types: list[MemoryType]
    auto_accept_memory_types: list[MemoryType]
    forbidden_sensitivity_levels: list[MemorySensitivity]
    maximum_retrieval_count: int
    maximum_context_tokens: int
    default_ttl_seconds: int | None
    review_role: str
    extraction_enabled: bool
    content_digest: str
    active: bool
    created_at: datetime

    @classmethod
    def create(
        cls,
        *,
        company_id: UUID,
        key: str,
        version: int,
        readable_namespace_patterns: list[str],
        writable_namespace_patterns: list[str],
        allowed_memory_types: list[MemoryType],
        auto_accept_memory_types: list[MemoryType] | None = None,
        forbidden_sensitivity_levels: list[MemorySensitivity] | None = None,
        maximum_retrieval_count: int = 10,
        maximum_context_tokens: int = 2_000,
        default_ttl_seconds: int | None = None,
        review_role: str = "company-owner",
        extraction_enabled: bool = False,
    ) -> MemoryPolicy:
        if version < 1:
            raise InvalidOrganizationalMemory("Memory Policy version must be positive")
        if not readable_namespace_patterns or not writable_namespace_patterns:
            raise InvalidOrganizationalMemory(
                "Memory Policy requires readable and writable namespace patterns"
            )
        if not allowed_memory_types:
            raise InvalidOrganizationalMemory(
                "Memory Policy requires at least one allowed Memory Type"
            )
        automatic = list(auto_accept_memory_types or [])
        if not set(automatic) <= set(allowed_memory_types):
            raise InvalidOrganizationalMemory(
                "Automatically accepted Memory Types must also be allowed"
            )
        if not 1 <= maximum_retrieval_count <= 100:
            raise InvalidOrganizationalMemory(
                "Maximum Memory retrieval count must be between 1 and 100"
            )
        if not 128 <= maximum_context_tokens <= 32_000:
            raise InvalidOrganizationalMemory(
                "Maximum Memory context tokens must be between 128 and 32000"
            )
        if default_ttl_seconds is not None and default_ttl_seconds < 60:
            raise InvalidOrganizationalMemory(
                "Default Memory TTL must be at least 60 seconds"
            )
        now = utc_now()
        policy = cls(
            id=uuid4(),
            company_id=company_id,
            key=_required(key, "Memory Policy key", 63).lower(),
            version=version,
            readable_namespace_patterns=sorted(set(readable_namespace_patterns)),
            writable_namespace_patterns=sorted(set(writable_namespace_patterns)),
            allowed_memory_types=sorted(
                set(allowed_memory_types), key=lambda value: value.value
            ),
            auto_accept_memory_types=sorted(
                set(automatic), key=lambda value: value.value
            ),
            forbidden_sensitivity_levels=sorted(
                set(forbidden_sensitivity_levels or []),
                key=lambda value: value.value,
            ),
            maximum_retrieval_count=maximum_retrieval_count,
            maximum_context_tokens=maximum_context_tokens,
            default_ttl_seconds=default_ttl_seconds,
            review_role=_required(review_role, "Memory review role", 128),
            extraction_enabled=extraction_enabled,
            content_digest="",
            active=True,
            created_at=now,
        )
        policy.content_digest = policy.calculate_digest()
        return policy

    def calculate_digest(self) -> str:
        return _digest(
            {
                key: (
                    [item.value for item in value]
                    if key
                    in {
                        "allowed_memory_types",
                        "auto_accept_memory_types",
                        "forbidden_sensitivity_levels",
                    }
                    else value
                )
                for key, value in self.__dict__.items()
                if key not in {"id", "content_digest", "active", "created_at"}
            }
        )

    def permits_namespace(
        self,
        namespace_type: MemoryNamespaceType,
        namespace_id: str,
        *,
        write: bool,
    ) -> bool:
        target = namespace_key(namespace_type, namespace_id)
        patterns = (
            self.writable_namespace_patterns
            if write
            else self.readable_namespace_patterns
        )
        return any(fnmatch.fnmatchcase(target, pattern) for pattern in patterns)


@dataclass
class MemoryRecord:
    id: UUID
    company_id: UUID
    namespace_type: MemoryNamespaceType
    namespace_id: str
    memory_type: MemoryType
    content: str
    content_digest: str
    provenance_type: MemoryProvenanceType
    provenance_id: str
    proposed_by_run_id: UUID | None
    reviewed_by: str | None
    confidence_basis_points: int
    sensitivity: MemorySensitivity
    status: MemoryStatus
    supersedes_id: UUID | None
    valid_from: datetime
    expires_at: datetime | None
    created_at: datetime
    accepted_at: datetime | None
    revoked_at: datetime | None

    @classmethod
    def propose(
        cls,
        *,
        company_id: UUID,
        namespace_type: MemoryNamespaceType,
        namespace_id: str,
        memory_type: MemoryType,
        content: str,
        provenance_type: MemoryProvenanceType,
        provenance_id: str,
        confidence_basis_points: int,
        sensitivity: MemorySensitivity,
        proposed_by_run_id: UUID | None = None,
        supersedes_id: UUID | None = None,
        expires_at: datetime | None = None,
    ) -> MemoryRecord:
        normalized_content = _required(content, "Memory content", 8_000)
        if any(pattern.search(normalized_content) for pattern in SECRET_PATTERNS):
            raise InvalidOrganizationalMemory(
                "Memory content appears to contain secret material"
            )
        if not 0 <= confidence_basis_points <= 10_000:
            raise InvalidOrganizationalMemory(
                "Memory confidence must be between 0 and 10000 basis points"
            )
        now = utc_now()
        if expires_at is not None:
            if expires_at.tzinfo is None or expires_at <= now:
                raise InvalidOrganizationalMemory(
                    "Memory expiry must be a future timezone-aware timestamp"
                )
        return cls(
            id=uuid4(),
            company_id=company_id,
            namespace_type=namespace_type,
            namespace_id=_required(namespace_id, "Memory namespace ID", 255),
            memory_type=memory_type,
            content=normalized_content,
            content_digest=_digest(normalized_content),
            provenance_type=provenance_type,
            provenance_id=_required(provenance_id, "Memory provenance ID", 255),
            proposed_by_run_id=proposed_by_run_id,
            reviewed_by=None,
            confidence_basis_points=confidence_basis_points,
            sensitivity=sensitivity,
            status=MemoryStatus.CANDIDATE,
            supersedes_id=supersedes_id,
            valid_from=now,
            expires_at=expires_at,
            created_at=now,
            accepted_at=None,
            revoked_at=None,
        )

    def accept(self, reviewer: str) -> None:
        self._require_candidate("accept")
        now = utc_now()
        self.status = MemoryStatus.ACCEPTED
        self.reviewed_by = _required(reviewer, "Memory reviewer", 128)
        self.accepted_at = now

    def reject(self, reviewer: str) -> None:
        self._require_candidate("reject")
        self.status = MemoryStatus.REJECTED
        self.reviewed_by = _required(reviewer, "Memory reviewer", 128)

    def supersede(self) -> None:
        if self.status is not MemoryStatus.ACCEPTED:
            raise InvalidOrganizationalMemory(
                f"Cannot supersede Memory from {self.status.value}"
            )
        self.status = MemoryStatus.SUPERSEDED

    def revoke(self, reviewer: str) -> None:
        if self.status is not MemoryStatus.ACCEPTED:
            raise InvalidOrganizationalMemory(
                f"Cannot revoke Memory from {self.status.value}"
            )
        self.status = MemoryStatus.REVOKED
        self.reviewed_by = _required(reviewer, "Memory revoker", 128)
        self.revoked_at = utc_now()

    def expire_if_due(self, now: datetime) -> bool:
        if (
            self.status is MemoryStatus.ACCEPTED
            and self.expires_at is not None
            and self.expires_at <= now
        ):
            self.status = MemoryStatus.EXPIRED
            return True
        return False

    def _require_candidate(self, action: str) -> None:
        if self.status is not MemoryStatus.CANDIDATE:
            raise InvalidOrganizationalMemory(
                f"Cannot {action} Memory from {self.status.value}"
            )


@dataclass(frozen=True)
class MemoryEvidence:
    memory_id: UUID
    evidence_type: str
    evidence_id: str
    evidence_digest: str | None
    created_at: datetime


@dataclass(frozen=True)
class MemoryReview:
    id: UUID
    memory_id: UUID
    decision: str
    reviewer: str
    reason: str
    created_at: datetime


@dataclass(frozen=True)
class MemoryRetrieval:
    id: UUID
    company_id: UUID
    policy_id: UUID
    policy_version: int
    query_digest: str
    namespace_keys: list[str]
    memory_types: list[MemoryType]
    result_memory_ids: list[UUID]
    reason: str
    principal_id: str
    task_id: UUID | None
    run_id: UUID | None
    created_at: datetime

    @classmethod
    def record(
        cls,
        *,
        company_id: UUID,
        policy: MemoryPolicy,
        query_digest: str,
        namespace_keys: list[str],
        memory_types: list[MemoryType],
        result_memory_ids: list[UUID],
        reason: str,
        principal_id: str,
        task_id: UUID | None,
        run_id: UUID | None,
    ) -> MemoryRetrieval:
        return cls(
            id=uuid4(),
            company_id=company_id,
            policy_id=policy.id,
            policy_version=policy.version,
            query_digest=query_digest,
            namespace_keys=namespace_keys,
            memory_types=memory_types,
            result_memory_ids=result_memory_ids,
            reason=_required(reason, "Memory retrieval reason", 1_000),
            principal_id=_required(principal_id, "Memory retrieval principal", 128),
            task_id=task_id,
            run_id=run_id,
            created_at=utc_now(),
        )


@dataclass(frozen=True)
class MemoryMatch:
    memory: MemoryRecord
    rank: int
    conflict: bool
