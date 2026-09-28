import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.application.company_services import CompanyModelService
from agentmesh.application.organizational_memory_services import (
    OrganizationalMemoryService,
)
from agentmesh.config import get_settings
from agentmesh.domain.organizational_memory import (
    MemoryNamespaceType,
    MemoryProvenanceType,
    MemorySensitivity,
    MemoryStatus,
    MemoryType,
)
from agentmesh.features import FeatureGateSet
from agentmesh.infrastructure.postgres.uow import SqlAlchemyUnitOfWorkFactory

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("AGENTMESH_RUN_POSTGRES_TESTS") != "1",
        reason="set AGENTMESH_RUN_POSTGRES_TESTS=1 to run service integration tests",
    ),
]


def test_memory_supersession_and_retrieval_evidence_round_trip_in_postgres() -> None:
    settings = get_settings()
    tenant_id = f"organizational-memory-{uuid4().hex}"
    engine = create_engine(settings.database_url)
    factory = SqlAlchemyUnitOfWorkFactory(
        sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    )
    gates = FeatureGateSet.from_config(
        "full", "company_model=true,organizational_memory=true"
    )
    company_service = CompanyModelService(
        uow_factory=factory, tenant_id=tenant_id, feature_gates=gates
    )
    service = OrganizationalMemoryService(
        uow_factory=factory, tenant_id=tenant_id, feature_gates=gates
    )
    try:
        company = company_service.create_company(
            name="Memory Integration Company",
            mission="Persist reviewed learning and exact retrieval evidence.",
            owner_principal_id="integration-owner",
        )
        policy = service.create_policy(
            company.id,
            key="integration",
            version=1,
            readable_namespace_patterns=[f"company/{company.id}"],
            writable_namespace_patterns=[f"company/{company.id}"],
            allowed_memory_types=[MemoryType.PROCEDURE],
            review_role="TENANT_ADMIN",
        )

        def propose(content: str, supersedes_id=None):
            return service.propose(
                company.id,
                policy_id=policy.id,
                namespace_type=MemoryNamespaceType.COMPANY,
                namespace_id=str(company.id),
                memory_type=MemoryType.PROCEDURE,
                content=content,
                provenance_type=MemoryProvenanceType.IMPORTED_POLICY,
                provenance_id="policy:integration",
                confidence_basis_points=10_000,
                sensitivity=MemorySensitivity.INTERNAL,
                evidence=[
                    {
                        "evidence_type": "policy",
                        "evidence_id": "integration",
                        "evidence_digest": "c" * 64,
                    }
                ],
                supersedes_id=supersedes_id,
                actor="integration-owner",
            )

        original = propose("Review reports monthly.")
        service.review(
            company.id,
            original.memory.id,
            policy_id=policy.id,
            decision="ACCEPT",
            reviewer="integration-owner",
            reviewer_roles={"TENANT_ADMIN"},
            reason="Initial procedure.",
        )
        replacement = propose(
            "Review reports weekly.", supersedes_id=original.memory.id
        )
        service.review(
            company.id,
            replacement.memory.id,
            policy_id=policy.id,
            decision="ACCEPT",
            reviewer="integration-owner",
            reviewer_roles={"TENANT_ADMIN"},
            reason="Approved cadence update.",
        )
        result = service.search(
            company.id,
            policy_id=policy.id,
            namespaces=[(MemoryNamespaceType.COMPANY, str(company.id))],
            memory_types=[MemoryType.PROCEDURE],
            query="weekly",
            reason="Integration context assembly.",
            principal_id="integration-agent",
        )

        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT m.status, count(e.memory_id) AS evidence_count "
                    "FROM memory_records m "
                    "LEFT JOIN memory_evidence e ON e.memory_id = m.id "
                    "WHERE m.id IN (:original_id, :replacement_id) "
                    "GROUP BY m.id, m.status ORDER BY m.status"
                ),
                {
                    "original_id": original.memory.id,
                    "replacement_id": replacement.memory.id,
                },
            ).all()
            retrieval_count = connection.execute(
                text(
                    "SELECT count(*) FROM memory_retrievals "
                    "WHERE company_id = :company_id"
                ),
                {"company_id": company.id},
            ).scalar_one()
        assert [row.status for row in rows] == ["ACCEPTED", "SUPERSEDED"]
        assert all(row.evidence_count == 1 for row in rows)
        assert [match.memory.id for match in result.matches] == [
            replacement.memory.id
        ]
        accepted = service.list_memories(
            company.id,
            statuses={MemoryStatus.ACCEPTED},
        )
        assert [item.memory.id for item in accepted] == [replacement.memory.id]
        assert retrieval_count == 1
    finally:
        engine.dispose()


def test_memory_onboarding_setup_and_manual_notes_persist_in_postgres() -> None:
    settings = get_settings()
    tenant_id = f"memory-onboarding-{uuid4().hex}"
    engine = create_engine(settings.database_url)
    factory = SqlAlchemyUnitOfWorkFactory(
        sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    )
    gates = FeatureGateSet.from_config(
        "full", "company_model=true,organizational_memory=true"
    )
    company_service = CompanyModelService(
        uow_factory=factory, tenant_id=tenant_id, feature_gates=gates
    )
    service = OrganizationalMemoryService(
        uow_factory=factory, tenant_id=tenant_id, feature_gates=gates
    )
    try:
        company = company_service.create_company(
            name="Memory Setup Integration Company",
            mission="Exercise explicit setup and durable manual notes.",
            owner_principal_id="integration-owner",
        )

        # Both requests race on the real PostgreSQL advisory lock. They must
        # return one shared, persisted policy instead of creating duplicates.
        barrier = Barrier(2)

        def setup_first_version():
            barrier.wait(timeout=10)
            return service.setup_default_policy(company.id)

        with ThreadPoolExecutor(max_workers=2) as executor:
            first, second = list(executor.map(lambda _index: setup_first_version(), range(2)))
        assert first.id == second.id
        assert first.version == 1
        assert service.setup_default_policy(company.id).id == first.id
        assert len(service.list_policies(company.id)) == 1

        updated = service.setup_default_policy(
            company.id, version=2, extraction_enabled=True
        )
        assert updated.id != first.id
        assert updated.version == 2
        assert updated.extraction_enabled is True
        persisted_policies = service.list_policies(company.id)
        assert len(persisted_policies) == 2
        assert next(value for value in persisted_policies if value.version == 1).active is False
        assert next(value for value in persisted_policies if value.version == 2).active is True

        note = service.propose_manual_note(
            company.id,
            policy_id=updated.id,
            content="Quarterly reports preserve an evidence link for each material claim.",
            actor="integration-owner",
        )
        assert note.memory.status is MemoryStatus.CANDIDATE
        query = {
            "policy_id": updated.id,
            "namespaces": [(MemoryNamespaceType.COMPANY, str(company.id))],
            "memory_types": [MemoryType.FACT],
            "query": "evidence link material claim",
            "reason": "Verify governed onboarding note retrieval.",
            "principal_id": "integration-agent",
        }
        pending_result = service.search(company.id, **query)
        assert pending_result.matches == []

        accepted = service.review(
            company.id,
            note.memory.id,
            policy_id=updated.id,
            decision="ACCEPT",
            reviewer="integration-owner",
            reviewer_roles={"TENANT_ADMIN"},
            reason="Verified against company reporting guidance.",
        )
        assert accepted.memory.status is MemoryStatus.ACCEPTED
        accepted_result = service.search(company.id, **query)
        assert [match.memory.id for match in accepted_result.matches] == [note.memory.id]

        revoked = service.revoke(
            company.id,
            note.memory.id,
            reviewer="integration-owner",
            reason="Company guidance was withdrawn.",
        )
        assert revoked.memory.status is MemoryStatus.REVOKED
        revoked_result = service.search(company.id, **query)
        assert revoked_result.matches == []

        with engine.connect() as connection:
            setup_policy_count = connection.execute(
                text(
                    "SELECT count(*) FROM memory_policies "
                    "WHERE company_id = :company_id AND key = :policy_key"
                ),
                {
                    "company_id": company.id,
                    "policy_key": service.DEFAULT_SETUP_PRESET,
                },
            ).scalar_one()
            persisted_reviews = connection.execute(
                text(
                    "SELECT decision FROM memory_reviews "
                    "WHERE memory_id = :memory_id ORDER BY created_at"
                ),
                {"memory_id": note.memory.id},
            ).scalars().all()
            retrieval_count = connection.execute(
                text(
                    "SELECT count(*) FROM memory_retrievals "
                    "WHERE company_id = :company_id"
                ),
                {"company_id": company.id},
            ).scalar_one()
        assert setup_policy_count == 2
        assert persisted_reviews == ["ACCEPT", "REVOKE"]
        assert retrieval_count == 3
    finally:
        engine.dispose()
