import os
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
        assert retrieval_count == 1
    finally:
        engine.dispose()
