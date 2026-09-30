import hashlib
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agentmesh.api.app import create_app
from agentmesh.application.company_services import CompanyModelService
from agentmesh.application.identity_services import IdentityService
from agentmesh.application.memory_runtime_services import RuntimeMemoryService
from agentmesh.application.organizational_memory_services import (
    OrganizationalMemoryService,
)
from agentmesh.application.ports import WorkflowExecutionResult
from agentmesh.application.services import RunExecutionService
from agentmesh.bootstrap import ApplicationContainer
from agentmesh.domain.errors import (
    FeatureDisabled,
    InvalidOrganizationalMemory,
    OrganizationalMemoryConflict,
    OrganizationalMemoryNotFound,
)
from agentmesh.domain.identity import Role
from agentmesh.domain.organizational_memory import (
    MemoryConflictStatus,
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
    extraction_enabled: bool = False,
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
        extraction_enabled=extraction_enabled,
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
    subject_key=None,
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
        subject_key=subject_key,
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
    accepted = _accept(organizational_memory_service, company.id, policy.id, first.memory.id)
    second = _propose(
        organizational_memory_service,
        company.id,
        policy.id,
        "Weekly reports should optimize speed even when source evidence is incomplete.",
    )
    _accept(organizational_memory_service, company.id, policy.id, second.memory.id)

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
    assert all(match.conflict is None for match in result.matches)
    assert all(match.conflict_status is MemoryConflictStatus.UNKNOWN for match in result.matches)
    assert result.matches[0].memory.id == first.memory.id
    assert result.retrieval.result_memory_ids == [match.memory.id for match in result.matches]
    assert organizational_memory_service.list_retrievals(company.id) == [result.retrieval]


@pytest.mark.parametrize(
    "keys, expected",
    [
        ((None, None), MemoryConflictStatus.UNKNOWN),
        (("release.label", "release.threshold"), MemoryConflictStatus.NO_COMPETING_RECORDS),
        (("release.label", "release.label"), MemoryConflictStatus.REVIEW_REQUIRED),
    ],
)
def test_distinct_content_is_not_a_confirmed_semantic_conflict(
    company_service: CompanyModelService,
    organizational_memory_service: OrganizationalMemoryService,
    keys: tuple[str | None, str | None],
    expected: MemoryConflictStatus,
) -> None:
    company, _ = _company(company_service)
    policy = _policy(organizational_memory_service, company.id)
    notes = []
    for key, content in zip(
        keys, ("Release labels use cobalt blue.", "Release requires a margin gate."), strict=True
    ):
        note = _propose(
            organizational_memory_service,
            company.id,
            policy.id,
            content,
            subject_key=key,
        )
        _accept(organizational_memory_service, company.id, policy.id, note.memory.id)
        notes.append(note)
    result = organizational_memory_service.search(
        company.id,
        policy_id=policy.id,
        namespaces=[(MemoryNamespaceType.COMPANY, str(company.id))],
        memory_types=[MemoryType.FACT],
        query="Release",
        reason="Conflict regression",
        principal_id="owner",
    )
    assert len(result.matches) == 2
    assert all(match.conflict_status is expected for match in result.matches)
    assert all(match.conflict is not True for match in result.matches)
    for match in result.matches:
        assert len(match.competing_memory_ids) == (
            1 if expected is MemoryConflictStatus.REVIEW_REQUIRED else 0
        )
    limited = organizational_memory_service.search(
        company.id,
        policy_id=policy.id,
        namespaces=[(MemoryNamespaceType.COMPANY, str(company.id))],
        memory_types=[MemoryType.FACT],
        query="Release",
        reason="Limited retrieval",
        principal_id="owner",
        maximum_count=1,
    )
    assert limited.matches[0].conflict_status is expected


def test_supersession_inherits_subject_and_excludes_old_record(
    company_service: CompanyModelService,
    organizational_memory_service: OrganizationalMemoryService,
) -> None:
    company, _ = _company(company_service)
    policy = _policy(organizational_memory_service, company.id)
    original = _propose(
        organizational_memory_service,
        company.id,
        policy.id,
        "Use blue labels.",
        subject_key=" Release.Label ",
    )
    assert original.memory.subject_key == "release.label"
    _accept(organizational_memory_service, company.id, policy.id, original.memory.id)
    with pytest.raises(OrganizationalMemoryConflict, match="subject key"):
        _propose(
            organizational_memory_service,
            company.id,
            policy.id,
            "Unrelated rule.",
            supersedes_id=original.memory.id,
            subject_key="another.topic",
        )
    replacement = _propose(
        organizational_memory_service,
        company.id,
        policy.id,
        "Use green labels.",
        supersedes_id=original.memory.id,
    )
    assert replacement.memory.subject_key == "release.label"
    _accept(organizational_memory_service, company.id, policy.id, replacement.memory.id)
    result = organizational_memory_service.search(
        company.id,
        policy_id=policy.id,
        namespaces=[(MemoryNamespaceType.COMPANY, str(company.id))],
        memory_types=[MemoryType.FACT],
        query="labels",
        reason="Supersession check",
        principal_id="owner",
    )
    assert [match.memory.id for match in result.matches] == [replacement.memory.id]
    assert result.matches[0].conflict_status is MemoryConflictStatus.NO_COMPETING_RECORDS
    assert result.matches[0].competing_memory_ids == ()


def test_policy_auto_accept_supersedes_original_atomically(
    company_service: CompanyModelService,
    organizational_memory_service: OrganizationalMemoryService,
) -> None:
    company, _ = _company(company_service)
    policy = _policy(organizational_memory_service, company.id, auto_accept=[MemoryType.FACT])
    original = _propose(
        organizational_memory_service,
        company.id,
        policy.id,
        "Review monthly.",
        subject_key="review.cadence",
    )
    replacement = _propose(
        organizational_memory_service,
        company.id,
        policy.id,
        "Review weekly.",
        supersedes_id=original.memory.id,
    )
    assert replacement.memory.status is MemoryStatus.ACCEPTED
    assert (
        organizational_memory_service.get_memory(company.id, original.memory.id).memory.status
        is MemoryStatus.SUPERSEDED
    )


@pytest.mark.parametrize("subject_key", ["", " ", "bad key", "x" * 129, "中文", 123])
def test_invalid_subject_keys_are_rejected(
    company_service: CompanyModelService,
    organizational_memory_service: OrganizationalMemoryService,
    subject_key,
) -> None:
    company, _ = _company(company_service)
    policy = _policy(organizational_memory_service, company.id)
    with pytest.raises(InvalidOrganizationalMemory, match="subject key"):
        _propose(
            organizational_memory_service,
            company.id,
            policy.id,
            "A note.",
            subject_key=subject_key,
        )


def test_candidate_and_other_namespace_do_not_create_competing_records(
    company_service: CompanyModelService,
    organizational_memory_service: OrganizationalMemoryService,
) -> None:
    company, unit = _company(company_service)
    policy = _policy(organizational_memory_service, company.id)
    first = _propose(
        organizational_memory_service,
        company.id,
        policy.id,
        "Company uses blue.",
        subject_key="label",
    )
    _accept(organizational_memory_service, company.id, policy.id, first.memory.id)
    _propose(
        organizational_memory_service,
        company.id,
        policy.id,
        "Pending green.",
        subject_key="label",
    )
    other = _propose(
        organizational_memory_service,
        company.id,
        policy.id,
        "Unit uses orange.",
        subject_key="label",
        namespace_type=MemoryNamespaceType.UNIT,
        namespace_id=str(unit.id),
    )
    _accept(organizational_memory_service, company.id, policy.id, other.memory.id)
    result = organizational_memory_service.search(
        company.id,
        policy_id=policy.id,
        namespaces=[
            (MemoryNamespaceType.COMPANY, str(company.id)),
            (MemoryNamespaceType.UNIT, str(unit.id)),
        ],
        memory_types=[MemoryType.FACT],
        query="uses",
        reason="Scope check",
        principal_id="owner",
    )
    assert len(result.matches) == 2
    assert all(
        match.conflict_status is MemoryConflictStatus.NO_COMPETING_RECORDS
        for match in result.matches
    )


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
    _accept(organizational_memory_service, company.id, writer.id, candidate.memory.id)
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
    _accept(organizational_memory_service, company.id, policy.id, original.memory.id)
    replacement = _propose(
        organizational_memory_service,
        company.id,
        policy.id,
        "The approved report cadence is weekly.",
        supersedes_id=original.memory.id,
    )
    _accept(organizational_memory_service, company.id, policy.id, replacement.memory.id)
    expiring = _propose(
        organizational_memory_service,
        company.id,
        policy.id,
        "Temporary launch guidance.",
        expires_at=utc_now() + timedelta(minutes=1),
    )
    _accept(organizational_memory_service, company.id, policy.id, expiring.memory.id)
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
    assert (
        organizational_memory_service.get_memory(company.id, original.memory.id).memory.status
        is MemoryStatus.SUPERSEDED
    )
    assert (
        organizational_memory_service.get_memory(company.id, expiring.memory.id).memory.status
        is MemoryStatus.EXPIRED
    )


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
        assert searched.json()["matches"][0]["conflict"] is None
        assert searched.json()["matches"][0]["conflict_status"] == "UNKNOWN"
        assert searched.json()["matches"][0]["competing_memory_ids"] == []
        retrievals = client.get(f"/api/v1/companies/{company.id}/memory/_retrievals")
        assert retrievals.status_code == 200
        assert retrievals.json()[0]["result_memory_ids"] == [memory_id]
        records = client.get(
            f"/api/v1/companies/{company.id}/memory/records",
            params={"status": "ACCEPTED"},
        )
        assert records.status_code == 200
        assert [item["memory"]["id"] for item in records.json()] == [memory_id]


def test_memory_onboarding_api_is_explicit_company_scoped_and_authz_gated(
    application_container,
    company_service: CompanyModelService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    company, _ = _company(company_service)
    foreign_company_service = CompanyModelService(
        uow_factory=uow_factory,
        tenant_id="foreign-tenant",
        feature_gates=FeatureGateSet.from_config("full", "company_model=true"),
    )
    foreign_company = foreign_company_service.create_company(
        name="Foreign Memory Company",
        mission="Tenant boundary test.",
        owner_principal_id="foreign-owner",
    )
    application_container.feature_gates = FeatureGateSet.from_config(
        "full", "company_model=true,organizational_memory=true"
    )
    read_token = "memory-read-token-0123456789abcdef"
    application_container.identity_service = IdentityService(
        enabled=True,
        tenant_id="test-tenant",
        principals_json=json.dumps(
            [
                {
                    "principal_id": "memory-reader",
                    "tenant_id": "test-tenant",
                    "roles": [Role.OPERATOR.value],
                    "token_sha256": hashlib.sha256(read_token.encode()).hexdigest(),
                }
            ]
        ),
    )
    with TestClient(create_app(application_container)) as client:
        base = f"/api/v1/companies/{company.id}/memory"
        assert client.get(f"{base}/setup").status_code == 401
        reader_headers = {"Authorization": f"Bearer {read_token}"}
        ready = client.get(f"{base}/setup", headers=reader_headers)
        assert ready.status_code == 200
        assert ready.json()["backend"] == "postgres-exact"
        assert ready.json()["configured"] is False
        assert ready.json()["policy"] is None
        assert client.get(f"{base}/policies", headers=reader_headers).json() == []
        presets = client.get(f"{base}/presets", headers=reader_headers)
        assert presets.status_code == 200
        assert presets.json()[0]["key"] == "reviewed_company_memory"
        assert presets.json()[0]["defaults"]["extraction_enabled"] is False

        assert client.post(f"{base}/setup", json={}, headers=reader_headers).status_code == 403
        admin_token = "memory-admin-token-0123456789abcdef"
        application_container.identity_service = IdentityService(
            enabled=True,
            tenant_id="test-tenant",
            principals_json=json.dumps(
                [
                    {
                        "principal_id": "memory-admin",
                        "tenant_id": "test-tenant",
                        "roles": [Role.TENANT_ADMIN.value],
                        "token_sha256": hashlib.sha256(admin_token.encode()).hexdigest(),
                    }
                ]
            ),
        )
        admin_headers = {"Authorization": f"Bearer {admin_token}"}
        setup = client.post(f"{base}/setup", json={}, headers=admin_headers)
        assert setup.status_code == 201
        policy_id = setup.json()["id"]
        assert setup.json()["review_role"] == "TENANT_ADMIN"
        assert setup.json()["extraction_enabled"] is False
        repeated = client.post(f"{base}/setup", json={}, headers=admin_headers)
        assert repeated.status_code == 201
        assert repeated.json()["id"] == policy_id

        note = client.post(
            f"{base}/notes",
            headers=admin_headers,
            json={
                "policy_id": policy_id,
                "content": "Manual company note: reports should retain source links.",
                "subject_key": " Report.Evidence ",
            },
        )
        assert note.status_code == 201
        note_value = note.json()
        assert note_value["memory"]["status"] == "CANDIDATE"
        assert note_value["memory"]["namespace_type"] == "COMPANY"
        assert note_value["memory"]["namespace_id"] == str(company.id)
        assert note_value["memory"]["subject_key"] == "report.evidence"
        assert note_value["memory"]["provenance_type"] == "USER_STATEMENT"
        assert note_value["evidence"][0]["evidence_type"] == "manual-note"

        reviewed = client.post(
            f"{base}/{note_value['memory']['id']}/review",
            headers=admin_headers,
            json={
                "policy_id": policy_id,
                "decision": "accept",
                "reason": "Checked with the company owner.",
            },
        )
        assert reviewed.status_code == 200
        assert reviewed.json()["memory"]["status"] == "ACCEPTED"

        # A read-only readiness check for another tenant cannot reveal or set it up.
        foreign_path = f"/api/v1/companies/{foreign_company.id}/memory"
        assert client.get(f"{foreign_path}/setup", headers=admin_headers).status_code == 404
        assert client.get(f"{foreign_path}/presets", headers=admin_headers).status_code == 404
        foreign_setup = client.post(f"{foreign_path}/setup", json={}, headers=admin_headers)
        assert foreign_setup.status_code == 404


class _RecordingMemoryWorkflow:
    def __init__(self, output):
        self.output = output
        self.work_items = []

    def run(self, task, run, attempt, work_item=None):
        self.work_items.append(work_item)
        return WorkflowExecutionResult(output=self.output)


class _RecordingRankingBackend:
    name = "recording-semantic"

    def __init__(self):
        self.candidate_ids = []

    def rank(self, query, candidates):
        del query
        self.candidate_ids = [candidate.id for candidate in candidates]
        return list(reversed(candidates))


def test_optional_ranking_backend_only_receives_authorized_canonical_records(
    company_service: CompanyModelService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    backend = _RecordingRankingBackend()
    service = OrganizationalMemoryService(
        uow_factory=uow_factory,
        tenant_id="test-tenant",
        feature_gates=FeatureGateSet.from_config(
            "full", "company_model=true,organizational_memory=true"
        ),
        ranking_backend=backend,
    )
    company, _ = _company(company_service)
    policy = _policy(service, company.id)
    accepted = _propose(
        service,
        company.id,
        policy.id,
        "Accepted canonical evidence.",
    )
    _accept(service, company.id, policy.id, accepted.memory.id)
    _propose(
        service,
        company.id,
        policy.id,
        "Unreviewed candidate must not leave the authorization boundary.",
    )

    result = service.search(
        company.id,
        policy_id=policy.id,
        namespaces=[(MemoryNamespaceType.COMPANY, str(company.id))],
        memory_types=[MemoryType.FACT],
        query="evidence",
        reason="Verify adapter trust boundary.",
        principal_id="owner",
    )

    assert service.backend_name == "recording-semantic"
    assert backend.candidate_ids == [accepted.memory.id]
    assert [match.memory.id for match in result.matches] == [accepted.memory.id]


def test_external_pilot_sync_exports_only_reviewed_policy_eligible_records(
    company_service: CompanyModelService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    company, _ = _company(company_service)

    class RecordingExternalBackend:
        name = "memos-cloud"
        allowed_company_id = company.id
        exported = []

        def sync(self, records):
            self.exported = list(records)
            return len(records)

        def rank(self, query, candidates):
            return candidates

    backend = RecordingExternalBackend()
    service = OrganizationalMemoryService(
        uow_factory=uow_factory,
        tenant_id="test-tenant",
        feature_gates=FeatureGateSet.from_config(
            "full", "company_model=true,organizational_memory=true"
        ),
        ranking_backend=backend,
    )
    policy = _policy(service, company.id)
    accepted = _propose(service, company.id, policy.id, "Reviewed safe memory for export.")
    _accept(service, company.id, policy.id, accepted.memory.id)
    _propose(service, company.id, policy.id, "Unreviewed memory stays only in PostgreSQL.")

    assert service.sync_external_pilot(company.id, policy_id=policy.id) == 1
    assert [item.id for item in backend.exported] == [accepted.memory.id]
    with pytest.raises(InvalidOrganizationalMemory, match="not enabled"):
        service.sync_external_pilot(uuid4(), policy_id=policy.id)


def test_external_pilot_api_requires_explicit_egress_acknowledgement(
    application_container: ApplicationContainer,
    company_service: CompanyModelService,
) -> None:
    company, _ = _company(company_service)
    service = application_container.organizational_memory_service
    policy = _policy(service, company.id)

    class EmptyExternalBackend:
        name = "memos-cloud"
        allowed_company_id = company.id

        def sync(self, records):
            assert records == []
            return 0

        def rank(self, query, candidates):
            return candidates

    service._ranking_backend = EmptyExternalBackend()
    application_container.feature_gates = FeatureGateSet.from_config(
        "full",
        "company_model=true,organizational_memory=true,identity_rbac=true,external_memory=true",
    )
    path = f"/api/v1/companies/{company.id}/memory/external/sync"
    with TestClient(create_app(application_container), base_url="https://testserver") as client:
        assert client.post(path, json={"policy_id": str(policy.id)}).status_code == 422
        response = client.post(
            path,
            json={"policy_id": str(policy.id), "acknowledge_remote_egress": True},
        )
    assert response.status_code == 200
    assert response.json() == {"backend": "memos-cloud", "synced_count": 0}


def test_runtime_injects_accepted_memory_and_captures_governed_candidates(
    company_service: CompanyModelService,
    organizational_memory_service: OrganizationalMemoryService,
    task_service,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    company, unit = _company(company_service)
    policy = _policy(
        organizational_memory_service,
        company.id,
        readable=[
            f"company/{company.id}",
            f"unit/{unit.id}",
            "project/*",
        ],
        writable=[f"company/{company.id}"],
        extraction_enabled=True,
    )
    existing = _propose(
        organizational_memory_service,
        company.id,
        policy.id,
        "Every material claim must preserve an attributable source.",
    )
    _accept(
        organizational_memory_service,
        company.id,
        policy.id,
        existing.memory.id,
    )
    runtime = RuntimeMemoryService(
        uow_factory=uow_factory,
        memory_service=organizational_memory_service,
        tenant_id="test-tenant",
        feature_gates=FeatureGateSet.from_config(
            "full", "company_model=true,organizational_memory=true"
        ),
    )
    workflow = _RecordingMemoryWorkflow(
        {
            "summary": "Completed with attributable evidence.",
            "memory_candidates": [
                {
                    "memory_type": "PATTERN",
                    "content": (
                        "Research plans are more reliable when evidence gaps "
                        "are assigned before drafting."
                    ),
                    "namespace_type": "COMPANY",
                    "namespace_id": str(company.id),
                    "confidence_basis_points": 8_500,
                    "sensitivity": "INTERNAL",
                }
            ],
        }
    )
    execution = RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=workflow,
        worker_id="memory-worker",
        consumer_name="memory-runner",
        lease_duration=timedelta(minutes=5),
        runtime_memory_service=runtime,
        feature_gates=FeatureGateSet.from_config(
            "full", "company_model=true,organizational_memory=true"
        ),
    )
    task = task_service.create_task(
        "Prepare an evidence-backed research plan",
        {
            "company_context": {
                "company_id": str(company.id),
                "organization_unit_id": str(unit.id),
                "memory_policy_id": str(policy.id),
            }
        },
    )
    task_service.request_run(task.task.id)

    assert execution.process(uow_factory.store.outbox[-1])

    work_item = workflow.work_items[0]
    assert work_item.input["agentmesh_memory"]["backend"] == "postgres-exact"
    assert work_item.input["agentmesh_memory"]["records"][0]["memory_id"] == str(existing.memory.id)
    assert work_item.input["agentmesh_memory"]["records"][0]["conflict"] is None
    assert work_item.input["agentmesh_memory"]["records"][0]["conflict_status"] == "UNKNOWN"
    assert "not a confirmed contradiction" in work_item.input["agentmesh_memory"]["instruction"]
    retrievals = organizational_memory_service.list_retrievals(company.id, task_id=task.task.id)
    assert len(retrievals) == 1
    assert retrievals[0].run_id is not None
    candidates = organizational_memory_service.list_candidates(company.id)
    assert len(candidates) == 1
    assert candidates[0].memory.memory_type is MemoryType.PATTERN
    assert candidates[0].memory.confidence_basis_points == 7_500
    assert candidates[0].memory.provenance_type is MemoryProvenanceType.TASK


def test_runtime_memory_is_optional_without_company_context(
    organizational_memory_service: OrganizationalMemoryService,
    task_service,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    runtime = RuntimeMemoryService(
        uow_factory=uow_factory,
        memory_service=organizational_memory_service,
        tenant_id="test-tenant",
        feature_gates=FeatureGateSet.from_config(
            "full", "company_model=true,organizational_memory=true"
        ),
    )
    workflow = _RecordingMemoryWorkflow({"summary": "No company scope."})
    execution = RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=workflow,
        worker_id="memory-worker",
        consumer_name="memory-runner",
        lease_duration=timedelta(minutes=5),
        runtime_memory_service=runtime,
    )
    task = task_service.create_task("A normal Task", {"scope": "local"})
    task_service.request_run(task.task.id)

    assert execution.process(uow_factory.store.outbox[-1])
    assert len(workflow.work_items) == 1
    assert workflow.work_items[0].objective == "A normal Task"
    assert workflow.work_items[0].input == {"scope": "local"}


def test_builtin_memory_setup_is_explicit_idempotent_and_versioned(
    company_service: CompanyModelService,
    organizational_memory_service: OrganizationalMemoryService,
) -> None:
    company, _ = _company(company_service)
    before = organizational_memory_service.setup_readiness(company.id)
    assert before["configured"] is False
    assert before["backend"] == "postgres-exact"
    assert before["policy"] is None
    assert before["external_backends"] == {"mem0": "deferred", "memos": "disabled"}

    policy = organizational_memory_service.setup_default_policy(company.id)
    assert policy.version == 1
    assert policy.readable_namespace_patterns == [f"company/{company.id}"]
    assert policy.writable_namespace_patterns == [f"company/{company.id}"]
    assert policy.auto_accept_memory_types == []
    assert policy.extraction_enabled is False
    assert policy.review_role == "TENANT_ADMIN"
    assert organizational_memory_service.setup_default_policy(company.id).id == policy.id

    with pytest.raises(OrganizationalMemoryConflict, match="next version"):
        organizational_memory_service.setup_default_policy(company.id, extraction_enabled=True)
    with pytest.raises(OrganizationalMemoryConflict, match="different settings"):
        organizational_memory_service.setup_default_policy(
            company.id, version=1, extraction_enabled=True
        )
    updated = organizational_memory_service.setup_default_policy(
        company.id, version=2, extraction_enabled=True
    )
    assert updated.version == 2
    assert updated.extraction_enabled is True
    assert any(
        value.version == 1 and not value.active
        for value in organizational_memory_service.list_policies(company.id)
    )
    assert organizational_memory_service.setup_readiness(company.id)["policy"].id == updated.id


def test_memory_setup_and_presets_are_tenant_scoped(
    company_service: CompanyModelService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    company, _ = _company(company_service)
    other_tenant = OrganizationalMemoryService(
        uow_factory=uow_factory,
        tenant_id="other-tenant",
        feature_gates=FeatureGateSet.from_config(
            "full", "company_model=true,organizational_memory=true"
        ),
    )
    with pytest.raises(OrganizationalMemoryNotFound):
        other_tenant.setup_readiness(company.id)
    with pytest.raises(OrganizationalMemoryNotFound):
        other_tenant.policy_presets(company.id)
    with pytest.raises(OrganizationalMemoryNotFound):
        other_tenant.setup_default_policy(company.id)


def test_next_correctly_scoped_task_recalls_only_accepted_nonrevoked_memory(
    company_service: CompanyModelService,
    organizational_memory_service: OrganizationalMemoryService,
    task_service,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    company, _ = _company(company_service)
    policy = organizational_memory_service.setup_default_policy(company.id)

    accepted = organizational_memory_service.propose_manual_note(
        company.id,
        policy_id=policy.id,
        content="Quarterly research reports use a source-linked evidence cadence.",
        actor="owner",
    )
    organizational_memory_service.review(
        company.id,
        accepted.memory.id,
        policy_id=policy.id,
        decision="ACCEPT",
        reviewer="owner",
        reviewer_roles={"TENANT_ADMIN"},
        reason="Verified against approved company guidance.",
    )
    unapproved = organizational_memory_service.propose_manual_note(
        company.id,
        policy_id=policy.id,
        content="Unreviewed draft says research reports should omit their sources.",
        actor="owner",
    )
    revoked = organizational_memory_service.propose_manual_note(
        company.id,
        policy_id=policy.id,
        content="Revoked note says research reports need no evidence trail.",
        actor="owner",
    )
    organizational_memory_service.review(
        company.id,
        revoked.memory.id,
        policy_id=policy.id,
        decision="ACCEPT",
        reviewer="owner",
        reviewer_roles={"TENANT_ADMIN"},
        reason="Temporarily accepted for revocation test.",
    )
    organizational_memory_service.revoke(
        company.id,
        revoked.memory.id,
        reviewer="owner",
        reason="Guidance was withdrawn.",
    )

    runtime = RuntimeMemoryService(
        uow_factory=uow_factory,
        memory_service=organizational_memory_service,
        tenant_id="test-tenant",
        feature_gates=FeatureGateSet.from_config(
            "full", "company_model=true,organizational_memory=true"
        ),
    )
    workflow = _RecordingMemoryWorkflow({"summary": "Task completed."})
    execution = RunExecutionService(
        uow_factory=uow_factory,
        workflow_runner=workflow,
        worker_id="memory-worker",
        consumer_name="memory-runner",
        lease_duration=timedelta(minutes=5),
        runtime_memory_service=runtime,
    )
    for _ in range(2):
        task = task_service.create_task(
            "Prepare quarterly research reports with source-linked evidence",
            {
                "company_context": {
                    "company_id": str(company.id),
                    "memory_policy_id": str(policy.id),
                }
            },
        )
        task_service.request_run(task.task.id)
        assert execution.process(uow_factory.store.outbox[-1])
        records = workflow.work_items[-1].input["agentmesh_memory"]["records"]
        assert [value["memory_id"] for value in records] == [str(accepted.memory.id)]
        assert str(unapproved.memory.id) not in {value["memory_id"] for value in records}
        assert str(revoked.memory.id) not in {value["memory_id"] for value in records}
