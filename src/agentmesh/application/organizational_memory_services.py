from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any
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


class OrganizationalMemoryService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        tenant_id: str,
        feature_gates: FeatureGateSet,
    ) -> None:
        self._uow_factory = uow_factory
        self._tenant_id = tenant_id
        self._feature_gates = feature_gates

    def create_policy(self, company_id: UUID, **values: Any) -> MemoryPolicy:
        self._require_enabled()
        policy = MemoryPolicy.create(company_id=company_id, **values)
        with self._uow_factory() as uow:
            self._active_company(uow, company_id)
            existing = uow.organizational_memory.get_policy_by_key(
                company_id, policy.key
            )
            if existing is not None and existing.version >= policy.version:
                raise OrganizationalMemoryConflict(
                    "Memory Policy version must increase"
                )
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
    ) -> MemorySnapshot:
        self._require_enabled()
        if not evidence:
            raise InvalidOrganizationalMemory(
                "Memory candidate requires durable evidence"
            )
        with self._uow_factory() as uow:
            self._active_company(uow, company_id)
            policy = self._policy(uow, company_id, policy_id)
            self._authorize_write(
                policy, namespace_type, namespace_id, memory_type, sensitivity
            )
            if supersedes_id is not None:
                original = self._memory(uow, company_id, supersedes_id)
                if original.status is not MemoryStatus.ACCEPTED:
                    raise OrganizationalMemoryConflict(
                        "Only an accepted Memory can be superseded"
                    )
                if (
                    original.namespace_type != namespace_type
                    or original.namespace_id != namespace_id
                    or original.memory_type != memory_type
                ):
                    raise OrganizationalMemoryConflict(
                        "Superseding Memory must retain namespace and type"
                    )
            if expires_at is None and policy.default_ttl_seconds is not None:
                expires_at = utc_now() + timedelta(
                    seconds=policy.default_ttl_seconds
                )
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
                raise OrganizationalMemoryConflict(
                    f"Duplicate Memory already exists as {duplicate.id}"
                )
            evidence_records = self._evidence(memory.id, evidence)
            uow.organizational_memory.add_record(memory)
            for item in evidence_records:
                uow.organizational_memory.add_evidence(item)
            if memory_type in policy.auto_accept_memory_types:
                memory.accept(actor)
                uow.organizational_memory.save_record(memory)
                uow.organizational_memory.add_review(
                    self._review(memory.id, "AUTO_ACCEPT", actor, "Memory Policy")
                )
            self._emit(
                uow,
                "memory.proposed",
                company_id,
                memory.id,
                self._event_payload(memory),
            )
            uow.commit()
            return MemorySnapshot(
                memory=memory,
                evidence=evidence_records,
                reviews=uow.organizational_memory.list_reviews(memory.id),
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
                    original = self._memory(
                        uow, company_id, memory.supersedes_id, for_update=True
                    )
                    original.supersede()
                    uow.organizational_memory.save_record(original)
            elif normalized == "REJECT":
                memory.reject(reviewer)
            else:
                raise InvalidOrganizationalMemory(
                    "Memory review decision must be ACCEPT or REJECT"
                )
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

    def get_memory(self, company_id: UUID, memory_id: UUID) -> MemorySnapshot:
        self._require_enabled()
        with self._uow_factory() as uow:
            self._company(uow, company_id)
            return self._snapshot(
                uow, self._memory(uow, company_id, memory_id)
            )

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
            raise InvalidOrganizationalMemory(
                "Memory search requires namespaces and Memory Types"
            )
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
                if not policy.permits_namespace(
                    namespace_type, namespace_id, write=False
                ):
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
            query_terms = {
                term for term in query.lower().split() if len(term) >= 2
            }
            ranked: list[tuple[int, MemoryRecord]] = []
            for memory in candidates:
                if memory.expire_if_due(evaluated_at):
                    uow.organizational_memory.save_record(memory)
                    continue
                if memory.status is not MemoryStatus.ACCEPTED:
                    continue
                if memory.sensitivity in policy.forbidden_sensitivity_levels:
                    continue
                score = sum(memory.content.lower().count(term) for term in query_terms)
                ranked.append((score, memory))
            ranked.sort(
                key=lambda item: (
                    -item[0],
                    -item[1].confidence_basis_points,
                    -item[1].accepted_at.timestamp() if item[1].accepted_at else 0,
                    str(item[1].id),
                )
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
            for _, memory in ranked:
                estimated = max(1, len(memory.content) // 4)
                if len(selected) >= count_limit or tokens + estimated > token_limit:
                    continue
                selected.append(memory)
                tokens += estimated
            conflicts = self._conflicts(selected)
            matches = [
                MemoryMatch(
                    memory=memory,
                    rank=index + 1,
                    conflict=memory.id in conflicts,
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
    def _evidence(
        memory_id: UUID, values: list[dict[str, str | None]]
    ) -> list[MemoryEvidence]:
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
            raise InvalidOrganizationalMemory(
                "Memory evidence type and ID are required"
            )
        return result

    @staticmethod
    def _review(
        memory_id: UUID, decision: str, reviewer: str, reason: str
    ) -> MemoryReview:
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
    def _conflicts(values: list[MemoryRecord]) -> set[UUID]:
        groups: dict[tuple[str, str, MemoryType], list[MemoryRecord]] = {}
        for value in values:
            groups.setdefault(
                (value.namespace_type.value, value.namespace_id, value.memory_type),
                [],
            ).append(value)
        return {
            value.id
            for group in groups.values()
            if len({value.content_digest for value in group}) > 1
            for value in group
        }

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
            "supersedes_id": (
                str(memory.supersedes_id) if memory.supersedes_id else None
            ),
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
            raise OrganizationalMemoryConflict(
                "Archived Company cannot manage Memory"
            )
        return company

    @staticmethod
    def _policy(uow: Any, company_id: UUID, policy_id: UUID) -> MemoryPolicy:
        policy = uow.organizational_memory.get_policy(policy_id)
        if policy is None or policy.company_id != company_id:
            raise OrganizationalMemoryNotFound(
                f"Memory Policy {policy_id} was not found"
            )
        return policy

    @staticmethod
    def _memory(
        uow: Any,
        company_id: UUID,
        memory_id: UUID,
        *,
        for_update: bool = False,
    ) -> MemoryRecord:
        memory = uow.organizational_memory.get_record(
            memory_id, for_update=for_update
        )
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
