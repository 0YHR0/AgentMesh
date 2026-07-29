from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from agentmesh.api.app import create_app
from agentmesh.application.company_services import CompanyModelService
from agentmesh.application.organizational_memory_services import (
    OrganizationalMemoryService,
)
from agentmesh.bootstrap import ApplicationContainer
from agentmesh.domain.errors import (
    FeatureDisabled,
    InvalidOrganizationalMemory,
    OrganizationalMemoryConflict,
)
from agentmesh.domain.organizational_memory import (
    MemoryNamespaceType,
    MemoryProvenanceType,
    MemorySensitivity,
    MemoryStatus,
    MemoryType,
)
from agentmesh.domain.tasks import utc_now
from agentmesh.features import FeatureGateSet
from tests.fakes import InMemoryUnitOfWorkFactory


def _company(company_service: CompanyModelService):
    company = company_service.create_company(
        name="Memory Company",
        mission="Learn from evidence without turning chat into truth.",
        owner_principal_id="owner",
    )
    unit = company_service.create_unit(
        company.id,
        key="research",
        name="Research",
        kind="department",
        purpose="Own evidence-backed learning.",
    )
    return company, unit


def _policy(
    service: OrganizationalMemoryService,
    company_id,
    *,
    key: str = "researcher",
    readable: list[str] | None = None,
    writable: list[str] | None = None,
    auto_accept: list[MemoryType] | None = None,
):
    return service.create_policy(
        company_id,
        key=key,
        version=1,
        readable_namespace_patterns=readable
        or [f"company/{company_id}", "unit/*", "employee/researcher"],
        writable_namespace_patterns=writable
        or [f"company/{company_id}", "unit/*", "employee/researcher"],
        allowed_memory_types=[
            MemoryType.FACT,
            MemoryType.PATTERN,
            MemoryType.PROCEDURE,
        ],
        auto_accept_memory_types=auto_accept or [],
        forbidden_sensitivity_levels=[MemorySensitivity.RESTRICTED],
        maximum_retrieval_count=5,
        maximum_context_tokens=512,
        review_role="TENANT_ADMIN",
    )


def _propose(
    service: OrganizationalMemoryService,
    company_id,
    policy_id,
    content: str,
    *,
    namespace_type: MemoryNamespaceType = MemoryNamespaceType.COMPANY,
    namespace_id: str | None = None,
    memory_type: MemoryType = MemoryType.FACT,
    expires_at=None,
    supersedes_id=None,
):
    return service.propose(
        company_id,
        policy_id=policy_id,
        namespace_type=namespace_type,
        namespace_id=namespace_id or str(company_id),
        memory_type=memory_type,
        content=content,
        provenance_type=MemoryProvenanceType.USER_STATEMENT,
        provenance_id="approved-statement:fixture",
        confidence_basis_points=9_000,
        sensitivity=MemorySensitivity.INTERNAL,
        evidence=[
            {
                "evidence_type": "approval",
                "evidence_id": "fixture",
                "evidence_digest": "a" * 64,
            }
        ],
        expires_at=expires_at,
        supersedes_id=supersedes_id,
        actor="owner",
    )


def _accept(
    service: OrganizationalMemoryService,
    company_id,
    policy_id,
    memory_id,
):
    return service.review(
        company_id,
        memory_id,
        policy_id=policy_id,
        decision="ACCEPT",
        reviewer="owner",
        reviewer_roles={"TENANT_ADMIN"},
        reason="Evidence reviewed.",
    )


def test_organizational_memory_requires_explicit_feature_gate(
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    service = OrganizationalMemoryService(
        uow_factory=uow_factory,
        tenant_id="test-tenant",
        feature_gates=FeatureGateSet.from_config("full", "company_model=true"),
    )

    with pytest.raises(FeatureDisabled, match="organizational_memory"):
        service.list_policies(next(iter(uow_factory.store.companies), None))


def test_candidate_review_search_and_retrieval_are_evidence_backed(
    company_service: CompanyModelService,
    organizational_memory_service: OrganizationalMemoryService,
) -> None:
    company, _ = _company(company_service)
    policy = _policy(organizational_memory_service, company.id)
    first = _propose(
        organizational_memory_service,
        company.id,
        policy.id,
        "Weekly reports perform best when every claim links to source evidence.",
    )
    assert first.memory.status is MemoryStatus.CANDIDATE
    accepted = _accept(
        organizational_memory_service, company.id, policy.id, first.memory.id
    )
    second = _propose(
        organizational_memory_service,
        company.id,
        policy.id,
        "Weekly reports should optimize speed even when source evidence is incomplete.",
    )
    _accept(
        organizational_memory_service, company.id, policy.id, second.memory.id
    )

    result = organizational_memory_service.search(
        company.id,
        policy_id=policy.id,
        namespaces=[(MemoryNamespaceType.COMPANY, str(company.id))],
        memory_types=[MemoryType.FACT],
        query="links claim",
        reason="Assemble a report Task context.",
        principal_id="agent:researcher",
    )

    assert accepted.memory.status is MemoryStatus.ACCEPTED
    assert [match.rank for match in result.matches] == [1, 2]
    assert all(match.conflict for match in result.matches)
    assert result.matches[0].memory.id == first.memory.id
    assert result.retrieval.result_memory_ids == [
        match.memory.id for match in result.matches
    ]
    assert organizational_memory_service.list_retrievals(company.id) == [
        result.retrieval
    ]


def test_namespace_authorization_precedes_retrieval(
    company_service: CompanyModelService,
    organizational_memory_service: OrganizationalMemoryService,
) -> None:
    company, _ = _company(company_service)
    writer = _policy(organizational_memory_service, company.id, key="writer")
    candidate = _propose(
        organizational_memory_service,
        company.id,
        writer.id,
        "Researcher-specific collaboration preference.",
        namespace_type=MemoryNamespaceType.EMPLOYEE,
        namespace_id="researcher",
        memory_type=MemoryType.PATTERN,
    )
    _accept(
        organizational_memory_service, company.id, writer.id, candidate.memory.id
    )
    analyst = _policy(
        organizational_memory_service,
        company.id,
        key="analyst",
        readable=["employee/analyst"],
        writable=["employee/analyst"],
    )

    with pytest.raises(OrganizationalMemoryConflict, match="denies namespace"):
        organizational_memory_service.search(
            company.id,
            policy_id=analyst.id,
            namespaces=[(MemoryNamespaceType.EMPLOYEE, "researcher")],
            memory_types=[MemoryType.PATTERN],
            query="collaboration",
            reason="Attempt cross-employee read.",
            principal_id="agent:analyst",
        )


def test_superseded_revoked_and_expired_memories_do_not_enter_future_context(
    company_service: CompanyModelService,
    organizational_memory_service: OrganizationalMemoryService,
) -> None:
    company, _ = _company(company_service)
    policy = _policy(organizational_memory_service, company.id)
    original = _propose(
        organizational_memory_service,
        company.id,
        policy.id,
        "The approved report cadence is monthly.",
    )
    _accept(
        organizational_memory_service, company.id, policy.id, original.memory.id
    )
    replacement = _propose(
        organizational_memory_service,
        company.id,
        policy.id,
        "The approved report cadence is weekly.",
        supersedes_id=original.memory.id,
    )
    _accept(
        organizational_memory_service, company.id, policy.id, replacement.memory.id
    )
    expiring = _propose(
        organizational_memory_service,
        company.id,
        policy.id,
        "Temporary launch guidance.",
        expires_at=utc_now() + timedelta(minutes=1),
    )
    _accept(
        organizational_memory_service, company.id, policy.id, expiring.memory.id
    )
    organizational_memory_service.revoke(
        company.id,
        replacement.memory.id,
        reviewer="owner",
        reason="Policy was withdrawn.",
    )

    result = organizational_memory_service.search(
        company.id,
        policy_id=policy.id,
        namespaces=[(MemoryNamespaceType.COMPANY, str(company.id))],
        memory_types=[MemoryType.FACT],
        query="report launch",
        reason="Verify lifecycle filtering.",
        principal_id="owner",
        now=utc_now() + timedelta(minutes=2),
    )

    assert result.matches == []
    assert organizational_memory_service.get_memory(
        company.id, original.memory.id
    ).memory.status is MemoryStatus.SUPERSEDED
    assert organizational_memory_service.get_memory(
        company.id, expiring.memory.id
    ).memory.status is MemoryStatus.EXPIRED


def test_secret_like_content_and_unreviewed_authority_fail_closed(
    company_service: CompanyModelService,
    organizational_memory_service: OrganizationalMemoryService,
) -> None:
    company, _ = _company(company_service)
    policy = _policy(organizational_memory_service, company.id)
    with pytest.raises(InvalidOrganizationalMemory, match="secret"):
        _propose(
            organizational_memory_service,
            company.id,
            policy.id,
            "api_key=sk_test_12345678901234567890",
        )
    candidate = _propose(
        organizational_memory_service,
        company.id,
        policy.id,
        "Candidate requiring review.",
    )
    with pytest.raises(OrganizationalMemoryConflict, match="requires role"):
        organizational_memory_service.review(
            company.id,
            candidate.memory.id,
            policy_id=policy.id,
            decision="ACCEPT",
            reviewer="agent:self",
            reviewer_roles={"OPERATOR"},
            reason="Self approval.",
        )


def test_memory_api_exposes_policy_review_search_and_audit(
    application_container: ApplicationContainer,
    company_service: CompanyModelService,
) -> None:
    company, _ = _company(company_service)
    application_container.feature_gates = FeatureGateSet.from_config(
        "full", "company_model=true,organizational_memory=true"
    )
    with TestClient(create_app(application_container)) as client:
        policy_response = client.post(
            f"/api/v1/companies/{company.id}/memory/policies",
            json={
                "key": "api-policy",
                "version": 1,
                "readable_namespace_patterns": [f"company/{company.id}"],
                "writable_namespace_patterns": [f"company/{company.id}"],
                "allowed_memory_types": ["FACT"],
                "review_role": "TENANT_ADMIN",
            },
        )
        assert policy_response.status_code == 201
        policy_id = policy_response.json()["id"]
        candidate_response = client.post(
            f"/api/v1/companies/{company.id}/memory/candidates",
            json={
                "policy_id": policy_id,
                "namespace_type": "COMPANY",
                "namespace_id": str(company.id),
                "memory_type": "FACT",
                "content": "API evidence-backed fact.",
                "provenance_type": "USER_STATEMENT",
                "provenance_id": "statement:api",
                "confidence_basis_points": 9000,
                "sensitivity": "INTERNAL",
                "evidence": [
                    {
                        "evidence_type": "approval",
                        "evidence_id": "api",
                        "evidence_digest": "b" * 64,
                    }
                ],
            },
        )
        assert candidate_response.status_code == 201
        memory_id = candidate_response.json()["memory"]["id"]
        reviewed = client.post(
            f"/api/v1/companies/{company.id}/memory/{memory_id}/review",
            json={
                "policy_id": policy_id,
                "decision": "accept",
                "reason": "API review.",
            },
        )
        assert reviewed.status_code == 200
        searched = client.post(
            f"/api/v1/companies/{company.id}/memory/_search",
            json={
                "policy_id": policy_id,
                "namespaces": [
                    {
                        "namespace_type": "COMPANY",
                        "namespace_id": str(company.id),
                    }
                ],
                "memory_types": ["FACT"],
                "query": "API fact",
                "reason": "API context assembly.",
            },
        )
        assert searched.status_code == 200
        assert searched.json()["matches"][0]["memory"]["id"] == memory_id
        retrievals = client.get(
            f"/api/v1/companies/{company.id}/memory/_retrievals"
        )
        assert retrievals.status_code == 200
        assert retrievals.json()[0]["result_memory_ids"] == [memory_id]
