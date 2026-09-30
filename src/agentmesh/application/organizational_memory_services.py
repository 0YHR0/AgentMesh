from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any, Protocol
from uuid import UUID, uuid4

from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.domain.company import CompanyStatus
from agentmesh.domain.errors import (
    InvalidOrganizationalMemory,
    OrganizationalMemoryConflict,
    OrganizationalMemoryNotFound,
)
from agentmesh.domain.messaging import MessageEnvelope
from agentmesh.domain.organizational_memory import (
    MemoryConflictStatus,
    MemoryEvidence,
    MemoryMatch,
    MemoryNamespaceType,
    MemoryPolicy,
    MemoryProvenanceType,
    MemoryRecord,
    MemoryRetrieval,
    MemoryReview,
    MemorySensitivity,
    MemoryStatus,
    MemoryType,
    namespace_key,
    normalize_subject_key,
)
from agentmesh.domain.tasks import utc_now
from agentmesh.features import Feature, FeatureGateSet


@dataclass(frozen=True)
class MemorySnapshot:
    memory: MemoryRecord
    evidence: list[MemoryEvidence]
    reviews: list[MemoryReview]


@dataclass(frozen=True)
class MemorySearchResult:
    matches: list[MemoryMatch]
    retrieval: MemoryRetrieval


class MemoryRankingBackend(Protocol):
    """Ranks already-authorized canonical Memory records."""

    name: str

    def rank(self, query: str, candidates: list[MemoryRecord]) -> list[MemoryRecord]: ...


class PostgresExactMemoryRankingBackend:
    name = "postgres-exact"

    def rank(self, query: str, candidates: list[MemoryRecord]) -> list[MemoryRecord]:
        query_terms = {term for term in query.lower().split() if len(term) >= 2}
        ranked = [
            (
                sum(memory.content.lower().count(term) for term in query_terms),
                memory,
            )
            for memory in candidates
        ]
        ranked.sort(
            key=lambda item: (
                -item[0],
                -item[1].confidence_basis_points,
                -item[1].accepted_at.timestamp() if item[1].accepted_at else 0,
                str(item[1].id),
            )
        )
        return [memory for _score, memory in ranked]


class OrganizationalMemoryService:
    DEFAULT_SETUP_PRESET = "reviewed_company_memory"

    def policy_presets(self, company_id: UUID) -> list[dict[str, Any]]:
        """Return safe, UI-consumable presets; company IDs are bound at setup time."""
        self._require_enabled()
        with self._uow_factory() as uow:
            self._company(uow, company_id)
        return [
            {
                "key": "reviewed_company_memory",
                "label": "Reviewed company memory",
                "description": (
                    "Company-wide notes are recalled only after an authorized "
                    "reviewer accepts them. Learning from completed tasks is off by default."
                ),
                "defaults": {
                    "readable_namespace_patterns": ["company/{company_id}"],
                    "writable_namespace_patterns": ["company/{company_id}"],
                    "allowed_memory_types": [item.value for item in MemoryType],
                    "auto_accept_memory_types": [],
                    "forbidden_sensitivity_levels": ["RESTRICTED"],
                    "maximum_retrieval_count": 5,
                    "maximum_context_tokens": 1_000,
                    "review_role": "TENANT_ADMIN",
                    "extraction_enabled": False,
                },
            }
        ]

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        tenant_id: str,
        feature_gates: FeatureGateSet,
        ranking_backend: MemoryRankingBackend | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._tenant_id = tenant_id
        self._feature_gates = feature_gates
        self._ranking_backend = ranking_backend or PostgresExactMemoryRankingBackend()

    @property
    def backend_name(self) -> str:
        return self._ranking_backend.name

    def create_policy(self, company_id: UUID, **values: Any) -> MemoryPolicy:
        self._require_enabled()
        policy = MemoryPolicy.create(company_id=company_id, **values)
        with self._uow_factory() as uow:
            self._active_company(uow, company_id)
            existing = uow.organizational_memory.get_policy_by_key(company_id, policy.key)
            if existing is not None and existing.version >= policy.version:
                raise OrganizationalMemoryConflict("Memory Policy version must increase")
            if existing is not None:
                existing.active = False
                uow.organizational_memory.save_policy(existing)
                uow.flush()
            uow.organizational_memory.add_policy(policy)
            self._emit(
                uow,
                "memory-policy.created",
                company_id,
                policy.id,
                {
                    "policy_version": policy.version,
                    "content_digest": policy.content_digest,
                },
            )
            uow.commit()
        return policy

    def setup_readiness(self, company_id: UUID) -> dict[str, Any]:
        """Read-only status for the built-in onboarding flow."""
        policies = self.list_policies(company_id)
        current = next(
            (
                value
                for value in policies
                if value.key == self.DEFAULT_SETUP_PRESET and value.active
            ),
            None,
        )
        return {
            "enabled": True,
            "backend": self.backend_name,
            "configured": current is not None,
            "policy": current,
            "recommended_preset": self.DEFAULT_SETUP_PRESET,
            "external_backends": {
                "mem0": "deferred",
                "memos": (
                    "pilot-enabled"
                    if getattr(self._ranking_backend, "allowed_company_id", None) == company_id
                    else "disabled"
                ),
            },
        }

    def sync_external_pilot(self, company_id: UUID, *, policy_id: UUID) -> int:
        """Explicitly rebuild the allowlisted Company's MemOS mirror.

        This is a bounded private-pilot operation, not an automatic export.
        Local acceptance, sensitivity and policy checks run before egress.
        """
        self._require_enabled()
        backend = self._ranking_backend
        if getattr(backend, "allowed_company_id", None) != company_id or not hasattr(
            backend, "sync"
        ):
            raise InvalidOrganizationalMemory("External Memory is not enabled for this Company")
        now = utc_now()
        with self._uow_factory() as uow:
            self._active_company(uow, company_id)
            policy = self._policy(uow, company_id, policy_id)
            if not policy.active:
                raise OrganizationalMemoryConflict("Memory Policy is inactive")
            records = uow.organizational_memory.list_records(company_id)
            eligible = [
                memory
                for memory in records
                if memory.status is MemoryStatus.ACCEPTED
                and (memory.expires_at is None or memory.expires_at > now)
                and memory.sensitivity
                in {
                    MemorySensitivity.PUBLIC,
                    MemorySensitivity.INTERNAL,
                }
                and memory.sensitivity not in policy.forbidden_sensitivity_levels
                and memory.memory_type in policy.allowed_memory_types
                and policy.permits_namespace(
                    memory.namespace_type, memory.namespace_id, write=False
                )
            ]
        if len(eligible) > 25:
            raise InvalidOrganizationalMemory(
                "The MemOS private pilot supports at most 25 eligible memories"
            )
        return backend.sync(eligible)

    def setup_default_policy(
        self,
        company_id: UUID,
        *,
        preset: str = DEFAULT_SETUP_PRESET,
        version: int | None = None,
        extraction_enabled: bool = False,
    ) -> MemoryPolicy:
        """Create/update the built-in policy explicitly and idempotently."""
        self._require_enabled()
        if preset != self.DEFAULT_SETUP_PRESET:
            raise InvalidOrganizationalMemory("Unknown Memory Policy preset")
        with self._uow_factory() as uow:
            # Keep lock and policy writes in the same transaction so parallel
            # first-time setup requests converge instead of racing at INSERT.
            uow.idempotency.lock("memory-setup", f"{company_id}:{preset}")
            self._active_company(uow, company_id)
            existing = uow.organizational_memory.get_policy_by_key(company_id, preset)
            values: dict[str, Any] = {
                "key": preset,
                "version": 1,
                "readable_namespace_patterns": [f"company/{company_id}"],
                "writable_namespace_patterns": [f"company/{company_id}"],
                "allowed_memory_types": list(MemoryType),
                "auto_accept_memory_types": [],
                "forbidden_sensitivity_levels": [MemorySensitivity.RESTRICTED],
                "maximum_retrieval_count": 5,
                "maximum_context_tokens": 1_000,
                "review_role": "TENANT_ADMIN",
                "extraction_enabled": extraction_enabled,
            }
            if existing is not None:
                if version is None or version == existing.version:
                    if existing.active and existing.extraction_enabled == extraction_enabled:
                        return existing
                    if version is not None and not existing.active:
                        raise OrganizationalMemoryConflict(
                            "Inactive Memory Policy version cannot be reactivated"
                        )
                    if version is not None and existing.active:
                        raise OrganizationalMemoryConflict(
                            "Memory Policy version already exists with different settings"
                        )
                    if version is None and existing.active:
                        raise OrganizationalMemoryConflict(
                            "Changing Memory Policy settings requires the next version"
                        )
                elif version != existing.version + 1:
                    raise OrganizationalMemoryConflict(
                        "Memory Policy version must be the next version"
                    )
            elif version not in (None, 1):
                raise OrganizationalMemoryConflict("Initial Memory Policy version must be 1")
            policy_version = version or (existing.version + 1 if existing else 1)
            values["version"] = policy_version
            policy = MemoryPolicy.create(company_id=company_id, **values)
            if existing is not None and existing.active:
                existing.active = False
                uow.organizational_memory.save_policy(existing)
                uow.flush()
            uow.organizational_memory.add_policy(policy)
            self._emit(
                uow,
                "memory-policy.created",
                company_id,
                policy.id,
                {
                    "policy_version": policy.version,
                    "content_digest": policy.content_digest,
                },
            )
            uow.commit()
            return policy

    def propose_manual_note(
        self,
        company_id: UUID,
        *,
        policy_id: UUID,
        content: str,
        actor: str,
        memory_type: MemoryType = MemoryType.FACT,
        subject_key: str | None = None,
    ) -> MemorySnapshot:
        """Create a company-scoped user note with server-derived evidence."""
        note_id = uuid4()
        digest = sha256(content.strip().encode()).hexdigest()
        return self.propose(
            company_id,
            policy_id=policy_id,
            namespace_type=MemoryNamespaceType.COMPANY,
            namespace_id=str(company_id),
            memory_type=memory_type,
            subject_key=subject_key,
            content=content,
            provenance_type=MemoryProvenanceType.USER_STATEMENT,
            provenance_id=f"manual-note:{note_id}",
            confidence_basis_points=8_000,
            sensitivity=MemorySensitivity.INTERNAL,
            evidence=[
                {
                    "evidence_type": "manual-note",
                    "evidence_id": str(note_id),
                    "evidence_digest": digest,
                }
            ],
            actor=actor,
        )

    def list_policies(self, company_id: UUID) -> list[MemoryPolicy]:
        self._require_enabled()
        with self._uow_factory() as uow:
            self._company(uow, company_id)
            return uow.organizational_memory.list_policies(company_id)

    def propose(
        self,
        company_id: UUID,
        *,
        policy_id: UUID,
        namespace_type: MemoryNamespaceType,
        namespace_id: str,
        memory_type: MemoryType,
        content: str,
        provenance_type: MemoryProvenanceType,
        provenance_id: str,
        confidence_basis_points: int,
        sensitivity: MemorySensitivity,
        evidence: list[dict[str, str | None]],
        proposed_by_run_id: UUID | None = None,
        supersedes_id: UUID | None = None,
        expires_at: datetime | None = None,
        actor: str,
        actor_roles: set[str] | None = None,
        subject_key: str | None = None,
    ) -> MemorySnapshot:
        self._require_enabled()
        with self._uow_factory() as uow:
            result = self.propose_in_unit_of_work(
                uow,
                company_id,
                policy_id=policy_id,
                namespace_type=namespace_type,
                namespace_id=namespace_id,
                memory_type=memory_type,
                content=content,
                provenance_type=provenance_type,
                provenance_id=provenance_id,
                confidence_basis_points=confidence_basis_points,
                sensitivity=sensitivity,
                evidence=evidence,
                proposed_by_run_id=proposed_by_run_id,
                supersedes_id=supersedes_id,
                expires_at=expires_at,
                actor=actor,
                actor_roles=actor_roles,
                subject_key=subject_key,
            )
            uow.commit()
            return result

    def propose_in_unit_of_work(
        self,
        uow: Any,
        company_id: UUID,
        *,
        policy_id: UUID,
        namespace_type: MemoryNamespaceType,
        namespace_id: str,
        memory_type: MemoryType,
        content: str,
        provenance_type: MemoryProvenanceType,
        provenance_id: str,
        confidence_basis_points: int,
        sensitivity: MemorySensitivity,
        evidence: list[dict[str, str | None]],
        proposed_by_run_id: UUID | None = None,
        supersedes_id: UUID | None = None,
        expires_at: datetime | None = None,
        actor: str,
        actor_roles: set[str] | None = None,
        subject_key: str | None = None,
    ) -> MemorySnapshot:
        del actor_roles
        self._require_enabled()
        if not evidence:
            raise InvalidOrganizationalMemory("Memory candidate requires durable evidence")
        self._active_company(uow, company_id)
        policy = self._policy(uow, company_id, policy_id)
        self._authorize_write(policy, namespace_type, namespace_id, memory_type, sensitivity)
        subject_key = normalize_subject_key(subject_key)
        if supersedes_id is not None:
            original = self._memory(uow, company_id, supersedes_id)
            if original.status is not MemoryStatus.ACCEPTED:
                raise OrganizationalMemoryConflict("Only an accepted Memory can be superseded")
            if (
                original.namespace_type != namespace_type
                or original.namespace_id != namespace_id
                or original.memory_type != memory_type
            ):
                raise OrganizationalMemoryConflict(
                    "Superseding Memory must retain namespace and type"
                )
            if subject_key is None:
                subject_key = original.subject_key
            elif original.subject_key is not None and subject_key != original.subject_key:
                raise OrganizationalMemoryConflict("Superseding Memory must retain its subject key")
        if expires_at is None and policy.default_ttl_seconds is not None:
            expires_at = utc_now() + timedelta(seconds=policy.default_ttl_seconds)
        memory = MemoryRecord.propose(
            company_id=company_id,
            namespace_type=namespace_type,
            namespace_id=namespace_id,
            memory_type=memory_type,
            content=content,
            provenance_type=provenance_type,
            provenance_id=provenance_id,
            confidence_basis_points=confidence_basis_points,
            sensitivity=sensitivity,
            proposed_by_run_id=proposed_by_run_id,
            supersedes_id=supersedes_id,
            expires_at=expires_at,
            subject_key=subject_key,
        )
        duplicate = uow.organizational_memory.find_by_digest(
            company_id=company_id,
            namespace_type=namespace_type.value,
            namespace_id=namespace_id,
            memory_type=memory_type,
            content_digest=memory.content_digest,
            statuses={MemoryStatus.CANDIDATE, MemoryStatus.ACCEPTED},
        )
        if duplicate is not None:
            raise OrganizationalMemoryConflict(f"Duplicate Memory already exists as {duplicate.id}")
        evidence_records = self._evidence(memory.id, evidence)
        uow.organizational_memory.add_record(memory)
        # Evidence is persisted through a separate repository operation, so
        # establish the parent row before PostgreSQL checks its foreign key.
        uow.flush()
        for item in evidence_records:
            uow.organizational_memory.add_evidence(item)
        reviews: list[MemoryReview] = []
        if memory_type in policy.auto_accept_memory_types:
            if supersedes_id is not None:
                original = self._memory(uow, company_id, supersedes_id, for_update=True)
                original.supersede()
                uow.organizational_memory.save_record(original)
            memory.accept(actor)
            uow.organizational_memory.save_record(memory)
            review = self._review(memory.id, "AUTO_ACCEPT", actor, "Memory Policy")
            uow.organizational_memory.add_review(review)
            reviews.append(review)
        self._emit(
            uow,
            "memory.proposed",
            company_id,
            memory.id,
            self._event_payload(memory),
        )
        return MemorySnapshot(
            memory=memory,
            evidence=evidence_records,
            reviews=reviews,
        )

    def review(
        self,
        company_id: UUID,
        memory_id: UUID,
        *,
        policy_id: UUID,
        decision: str,
        reviewer: str,
        reviewer_roles: set[str],
        reason: str,
    ) -> MemorySnapshot:
        self._require_enabled()
        with self._uow_factory() as uow:
            self._active_company(uow, company_id)
            policy = self._policy(uow, company_id, policy_id)
            if not policy.active:
                raise OrganizationalMemoryConflict("Memory Policy is inactive")
            if policy.review_role not in reviewer_roles:
                raise OrganizationalMemoryConflict(
                    f"Memory review requires role '{policy.review_role}'"
                )
            memory = self._memory(uow, company_id, memory_id, for_update=True)
            normalized = decision.strip().upper()
            if normalized == "ACCEPT":
                memory.accept(reviewer)
                if memory.supersedes_id is not None:
                    original = self._memory(uow, company_id, memory.supersedes_id, for_update=True)
                    original.supersede()
                    uow.organizational_memory.save_record(original)
            elif normalized == "REJECT":
                memory.reject(reviewer)
            else:
                raise InvalidOrganizationalMemory("Memory review decision must be ACCEPT or REJECT")
            uow.organizational_memory.save_record(memory)
            uow.organizational_memory.add_review(
                self._review(memory.id, normalized, reviewer, reason)
            )
            self._emit(
                uow,
                f"memory.{normalized.lower()}ed",
                company_id,
                memory.id,
                self._event_payload(memory),
            )
            uow.commit()
            return self._snapshot(uow, memory)

    def revoke(
        self,
        company_id: UUID,
        memory_id: UUID,
        *,
        reviewer: str,
        reason: str,
    ) -> MemorySnapshot:
        self._require_enabled()
        with self._uow_factory() as uow:
            self._active_company(uow, company_id)
            memory = self._memory(uow, company_id, memory_id, for_update=True)
            memory.revoke(reviewer)
            uow.organizational_memory.save_record(memory)
            uow.organizational_memory.add_review(
                self._review(memory.id, "REVOKE", reviewer, reason)
            )
            self._emit(
                uow,
                "memory.revoked",
                company_id,
                memory.id,
                self._event_payload(memory),
            )
            uow.commit()
            return self._snapshot(uow, memory)

    def list_candidates(self, company_id: UUID) -> list[MemorySnapshot]:
        self._require_enabled()
        with self._uow_factory() as uow:
            self._company(uow, company_id)
            return [
                self._snapshot(uow, memory)
                for memory in uow.organizational_memory.list_candidates(company_id)
            ]

    def list_memories(
        self,
        company_id: UUID,
        *,
        statuses: set[MemoryStatus] | None = None,
        now: datetime | None = None,
    ) -> list[MemorySnapshot]:
        self._require_enabled()
        evaluated_at = now or utc_now()
        with self._uow_factory() as uow:
            self._company(uow, company_id)
            memories = uow.organizational_memory.list_records(
                company_id,
                statuses=statuses,
            )
            changed = False
            for memory in memories:
                if memory.expire_if_due(evaluated_at):
                    uow.organizational_memory.save_record(memory)
                    changed = True
            if changed:
                uow.commit()
            return [self._snapshot(uow, memory) for memory in memories]

    def get_memory(self, company_id: UUID, memory_id: UUID) -> MemorySnapshot:
        self._require_enabled()
        with self._uow_factory() as uow:
            self._company(uow, company_id)
            return self._snapshot(uow, self._memory(uow, company_id, memory_id))

    def search(
        self,
        company_id: UUID,
        *,
        policy_id: UUID,
        namespaces: list[tuple[MemoryNamespaceType, str]],
        memory_types: list[MemoryType],
        query: str,
        reason: str,
        principal_id: str,
        maximum_count: int | None = None,
        maximum_context_tokens: int | None = None,
        task_id: UUID | None = None,
        run_id: UUID | None = None,
        now: datetime | None = None,
    ) -> MemorySearchResult:
        self._require_enabled()
        if not namespaces or not memory_types:
            raise InvalidOrganizationalMemory("Memory search requires namespaces and Memory Types")
        evaluated_at = now or utc_now()
        with self._uow_factory() as uow:
            self._company(uow, company_id)
            policy = self._policy(uow, company_id, policy_id)
            if not policy.active:
                raise OrganizationalMemoryConflict("Memory Policy is inactive")
            if not set(memory_types) <= set(policy.allowed_memory_types):
                raise OrganizationalMemoryConflict(
                    "Memory search requested a disallowed Memory Type"
                )
            keys = []
            for namespace_type, namespace_id in namespaces:
                if not policy.permits_namespace(namespace_type, namespace_id, write=False):
                    raise OrganizationalMemoryConflict(
                        f"Memory Policy denies namespace "
                        f"{namespace_key(namespace_type, namespace_id)}"
                    )
                keys.append(namespace_key(namespace_type, namespace_id))
            candidates = uow.organizational_memory.search_records(
                company_id=company_id,
                namespace_keys=keys,
                memory_types=memory_types,
            )
            authorized: list[MemoryRecord] = []
            for memory in candidates:
                if memory.expire_if_due(evaluated_at):
                    uow.organizational_memory.save_record(memory)
                    continue
                if memory.status is not MemoryStatus.ACCEPTED:
                    continue
                if memory.sensitivity in policy.forbidden_sensitivity_levels:
                    continue
                authorized.append(memory)
            ranked = self._ranking_backend.rank(query, authorized)
            if {value.id for value in ranked} != {value.id for value in authorized} or len(
                ranked
            ) != len(authorized):
                raise InvalidOrganizationalMemory(
                    "Memory ranking backend returned an invalid candidate set"
                )
            count_limit = min(
                maximum_count or policy.maximum_retrieval_count,
                policy.maximum_retrieval_count,
            )
            token_limit = min(
                maximum_context_tokens or policy.maximum_context_tokens,
                policy.maximum_context_tokens,
            )
            selected: list[MemoryRecord] = []
            tokens = 0
            for memory in ranked:
                estimated = max(1, len(memory.content) // 4)
                if len(selected) >= count_limit or tokens + estimated > token_limit:
                    continue
                selected.append(memory)
                tokens += estimated
            assessments = self._conflict_assessments(authorized)
            matches = [
                MemoryMatch(
                    memory=memory,
                    rank=index + 1,
                    conflict=(
                        False
                        if assessments[memory.id][0] is MemoryConflictStatus.NO_COMPETING_RECORDS
                        else None
                    ),
                    conflict_status=assessments[memory.id][0],
                    conflict_reason=assessments[memory.id][1],
                    competing_memory_ids=assessments[memory.id][2],
                )
                for index, memory in enumerate(selected)
            ]
            query_digest = sha256(
                json.dumps(
                    {
                        "query": query,
                        "namespaces": keys,
                        "memory_types": [item.value for item in memory_types],
                        "maximum_count": count_limit,
                        "maximum_context_tokens": token_limit,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            retrieval = MemoryRetrieval.record(
                company_id=company_id,
                policy=policy,
                query_digest=query_digest,
                namespace_keys=keys,
                memory_types=memory_types,
                result_memory_ids=[memory.id for memory in selected],
                reason=reason,
                principal_id=principal_id,
                task_id=task_id,
                run_id=run_id,
            )
            uow.organizational_memory.add_retrieval(retrieval)
            self._emit(
                uow,
                "memory.retrieved",
                company_id,
                retrieval.id,
                {
                    "policy_id": str(policy.id),
                    "policy_version": policy.version,
                    "query_digest": query_digest,
                    "result_count": len(selected),
                    "task_id": str(task_id) if task_id else None,
                    "run_id": str(run_id) if run_id else None,
                },
            )
            uow.commit()
            return MemorySearchResult(matches=matches, retrieval=retrieval)

    def list_retrievals(
        self,
        company_id: UUID,
        *,
        task_id: UUID | None = None,
        run_id: UUID | None = None,
    ) -> list[MemoryRetrieval]:
        self._require_enabled()
        with self._uow_factory() as uow:
            self._company(uow, company_id)
            return [
                value
                for value in uow.organizational_memory.list_retrievals(
                    task_id=task_id, run_id=run_id
                )
                if value.company_id == company_id
            ]

    @staticmethod
    def _evidence(memory_id: UUID, values: list[dict[str, str | None]]) -> list[MemoryEvidence]:
        if len(values) > 20:
            raise InvalidOrganizationalMemory(
                "Memory candidate supports at most 20 evidence references"
            )
        result = []
        for value in values:
            result.append(
                MemoryEvidence(
                    memory_id=memory_id,
                    evidence_type=str(value.get("evidence_type", "")).strip(),
                    evidence_id=str(value.get("evidence_id", "")).strip(),
                    evidence_digest=(
                        str(value["evidence_digest"]).strip()
                        if value.get("evidence_digest")
                        else None
                    ),
                    created_at=utc_now(),
                )
            )
        if any(not item.evidence_type or not item.evidence_id for item in result):
            raise InvalidOrganizationalMemory("Memory evidence type and ID are required")
        return result

    @staticmethod
    def _review(memory_id: UUID, decision: str, reviewer: str, reason: str) -> MemoryReview:
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise InvalidOrganizationalMemory("Memory review reason is required")
        return MemoryReview(
            id=uuid4(),
            memory_id=memory_id,
            decision=decision,
            reviewer=reviewer,
            reason=normalized_reason,
            created_at=utc_now(),
        )

    @staticmethod
    def _conflict_assessments(
        values: list[MemoryRecord],
    ) -> dict[UUID, tuple[MemoryConflictStatus, str, tuple[UUID, ...]]]:
        """Detect competing scoped records, never infer semantic contradiction from text."""
        groups: dict[tuple[UUID, str, str, MemoryType, str], list[MemoryRecord]] = {}
        for value in values:
            if value.subject_key is None:
                continue
            groups.setdefault(
                (
                    value.company_id,
                    value.namespace_type.value,
                    value.namespace_id,
                    value.memory_type,
                    value.subject_key,
                ),
                [],
            ).append(value)
        result = {
            value.id: (MemoryConflictStatus.UNKNOWN, "missing_subject_key", ()) for value in values
        }
        for group in groups.values():
            for value in group:
                competing = tuple(
                    sorted(
                        (
                            other.id
                            for other in group
                            if other.content_digest != value.content_digest
                        ),
                        key=str,
                    )
                )
                result[value.id] = (
                    MemoryConflictStatus.REVIEW_REQUIRED
                    if competing
                    else MemoryConflictStatus.NO_COMPETING_RECORDS,
                    "multiple_active_contents" if competing else "no_competing_active_contents",
                    competing,
                )
        return result

    @staticmethod
    def _event_payload(memory: MemoryRecord) -> dict[str, Any]:
        return {
            "memory_id": str(memory.id),
            "namespace_type": memory.namespace_type.value,
            "namespace_id_digest": sha256(memory.namespace_id.encode()).hexdigest(),
            "memory_type": memory.memory_type.value,
            "content_digest": memory.content_digest,
            "sensitivity": memory.sensitivity.value,
            "status": memory.status.value,
            "supersedes_id": (str(memory.supersedes_id) if memory.supersedes_id else None),
        }

    @staticmethod
    def _snapshot(uow: Any, memory: MemoryRecord) -> MemorySnapshot:
        return MemorySnapshot(
            memory=memory,
            evidence=uow.organizational_memory.list_evidence(memory.id),
            reviews=uow.organizational_memory.list_reviews(memory.id),
        )

    @staticmethod
    def _authorize_write(
        policy: MemoryPolicy,
        namespace_type: MemoryNamespaceType,
        namespace_id: str,
        memory_type: MemoryType,
        sensitivity: MemorySensitivity,
    ) -> None:
        if not policy.active:
            raise OrganizationalMemoryConflict("Memory Policy is inactive")
        if not policy.permits_namespace(namespace_type, namespace_id, write=True):
            raise OrganizationalMemoryConflict("Memory Policy denies write namespace")
        if memory_type not in policy.allowed_memory_types:
            raise OrganizationalMemoryConflict("Memory Policy denies Memory Type")
        if sensitivity in policy.forbidden_sensitivity_levels:
            raise OrganizationalMemoryConflict("Memory Policy denies sensitivity")

    def _company(self, uow: Any, company_id: UUID):
        company = uow.company_model.get_company(company_id)
        if company is None or company.tenant_id != self._tenant_id:
            raise OrganizationalMemoryNotFound(f"Company {company_id} was not found")
        return company

    def _active_company(self, uow: Any, company_id: UUID):
        company = self._company(uow, company_id)
        if company.status is not CompanyStatus.ACTIVE:
            raise OrganizationalMemoryConflict("Archived Company cannot manage Memory")
        return company

    @staticmethod
    def _policy(uow: Any, company_id: UUID, policy_id: UUID) -> MemoryPolicy:
        policy = uow.organizational_memory.get_policy(policy_id)
        if policy is None or policy.company_id != company_id:
            raise OrganizationalMemoryNotFound(f"Memory Policy {policy_id} was not found")
        return policy

    @staticmethod
    def _memory(
        uow: Any,
        company_id: UUID,
        memory_id: UUID,
        *,
        for_update: bool = False,
    ) -> MemoryRecord:
        memory = uow.organizational_memory.get_record(memory_id, for_update=for_update)
        if memory is None or memory.company_id != company_id:
            raise OrganizationalMemoryNotFound(f"Memory {memory_id} was not found")
        return memory

    def _require_enabled(self) -> None:
        self._feature_gates.require(Feature.ORGANIZATIONAL_MEMORY)

    def _emit(
        self,
        uow: Any,
        suffix: str,
        company_id: UUID,
        aggregate_id: UUID,
        payload: dict[str, Any],
    ) -> None:
        uow.outbox.add(
            MessageEnvelope.domain_event(
                schema_name=f"agentmesh.company.{suffix}",
                tenant_id=self._tenant_id,
                aggregate_id=aggregate_id,
                payload={"company_id": str(company_id), **payload},
            )
        )
