from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agentmesh.application.organizational_memory_services import (
    MemorySearchResult,
    MemorySnapshot,
)
from agentmesh.domain.organizational_memory import (
    MemoryNamespaceType,
    MemoryProvenanceType,
    MemorySensitivity,
    MemoryStatus,
    MemoryType,
)


class CreateMemoryPolicyRequest(BaseModel):
    key: str = Field(min_length=1, max_length=63)
    version: int = Field(ge=1)
    readable_namespace_patterns: list[str] = Field(min_length=1)
    writable_namespace_patterns: list[str] = Field(min_length=1)
    allowed_memory_types: list[MemoryType] = Field(min_length=1)
    auto_accept_memory_types: list[MemoryType] = Field(default_factory=list)
    forbidden_sensitivity_levels: list[MemorySensitivity] = Field(
        default_factory=list
    )
    maximum_retrieval_count: int = Field(default=10, ge=1, le=100)
    maximum_context_tokens: int = Field(default=2_000, ge=128, le=32_000)
    default_ttl_seconds: int | None = Field(default=None, ge=60)
    review_role: str = Field(default="company-owner", min_length=1, max_length=128)
    extraction_enabled: bool = False


class MemoryEvidenceRequest(BaseModel):
    evidence_type: str = Field(min_length=1, max_length=63)
    evidence_id: str = Field(min_length=1, max_length=255)
    evidence_digest: str | None = Field(default=None, min_length=64, max_length=64)


class ProposeMemoryRequest(BaseModel):
    policy_id: UUID
    namespace_type: MemoryNamespaceType
    namespace_id: str = Field(min_length=1, max_length=255)
    memory_type: MemoryType
    content: str = Field(min_length=1, max_length=8_000)
    provenance_type: MemoryProvenanceType
    provenance_id: str = Field(min_length=1, max_length=255)
    confidence_basis_points: int = Field(ge=0, le=10_000)
    sensitivity: MemorySensitivity
    evidence: list[MemoryEvidenceRequest] = Field(min_length=1, max_length=20)
    proposed_by_run_id: UUID | None = None
    supersedes_id: UUID | None = None
    expires_at: datetime | None = None


class ReviewMemoryRequest(BaseModel):
    policy_id: UUID
    decision: str = Field(pattern="^(?i:accept|reject)$")
    reason: str = Field(min_length=1, max_length=4_000)


class RevokeMemoryRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=4_000)


class MemoryNamespaceRequest(BaseModel):
    namespace_type: MemoryNamespaceType
    namespace_id: str = Field(min_length=1, max_length=255)


class SearchMemoryRequest(BaseModel):
    policy_id: UUID
    namespaces: list[MemoryNamespaceRequest] = Field(min_length=1, max_length=50)
    memory_types: list[MemoryType] = Field(min_length=1)
    query: str = Field(default="", max_length=2_000)
    reason: str = Field(min_length=1, max_length=1_000)
    maximum_count: int | None = Field(default=None, ge=1, le=100)
    maximum_context_tokens: int | None = Field(default=None, ge=128, le=32_000)
    task_id: UUID | None = None
    run_id: UUID | None = None


class MemoryPolicyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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


class MemoryRecordResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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


class MemoryEvidenceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    memory_id: UUID
    evidence_type: str
    evidence_id: str
    evidence_digest: str | None
    created_at: datetime


class MemoryReviewResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    memory_id: UUID
    decision: str
    reviewer: str
    reason: str
    created_at: datetime


class MemorySnapshotResponse(BaseModel):
    memory: MemoryRecordResponse
    evidence: list[MemoryEvidenceResponse]
    reviews: list[MemoryReviewResponse]

    @classmethod
    def from_snapshot(cls, value: MemorySnapshot) -> "MemorySnapshotResponse":
        return cls(
            memory=MemoryRecordResponse.model_validate(value.memory),
            evidence=[
                MemoryEvidenceResponse.model_validate(item)
                for item in value.evidence
            ],
            reviews=[
                MemoryReviewResponse.model_validate(item) for item in value.reviews
            ],
        )


class MemoryMatchResponse(BaseModel):
    memory: MemoryRecordResponse
    rank: int
    conflict: bool


class MemoryRetrievalResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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


class MemorySearchResponse(BaseModel):
    matches: list[MemoryMatchResponse]
    retrieval: MemoryRetrievalResponse

    @classmethod
    def from_result(cls, value: MemorySearchResult) -> "MemorySearchResponse":
        return cls(
            matches=[
                MemoryMatchResponse(
                    memory=MemoryRecordResponse.model_validate(item.memory),
                    rank=item.rank,
                    conflict=item.conflict,
                )
                for item in value.matches
            ],
            retrieval=MemoryRetrievalResponse.model_validate(value.retrieval),
        )
