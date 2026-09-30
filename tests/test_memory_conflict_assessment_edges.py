import json
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from agentmesh.application.organizational_memory_services import (
    OrganizationalMemoryService,
    memory_context_record,
)
from agentmesh.domain.errors import OrganizationalMemoryConflict
from agentmesh.domain.organizational_memory import (
    MemoryConflictStatus,
    MemoryNamespaceType,
    MemoryProvenanceType,
    MemorySensitivity,
    MemoryStatus,
    MemoryType,
)
from agentmesh.domain.tasks import utc_now


def _company(company_service):
    return company_service.create_company(
        name="Conflict assessment edges",
        mission="Keep competing-record assessment within its authorization boundary.",
        owner_principal_id="owner",
    )


def _policy(service, company_id, *, key="writer", allowed=None, forbidden=None):
    return service.create_policy(
        company_id,
        key=key,
        version=1,
        readable_namespace_patterns=["*"],
        writable_namespace_patterns=["*"],
        allowed_memory_types=allowed or list(MemoryType),
        forbidden_sensitivity_levels=forbidden or [],
        maximum_retrieval_count=10,
        review_role="TENANT_ADMIN",
    )


def _note(service, company_id, policy_id, content, **overrides):
    values = {
        "namespace_type": MemoryNamespaceType.COMPANY,
        "namespace_id": str(company_id),
        "memory_type": MemoryType.FACT,
        "provenance_type": MemoryProvenanceType.USER_STATEMENT,
        "provenance_id": "edge-test:approved-note",
        "confidence_basis_points": 9_000,
        "sensitivity": MemorySensitivity.INTERNAL,
        "evidence": [{"evidence_type": "approval", "evidence_id": "edge-fixture"}],
        "subject_key": "report.cadence",
        "actor": "owner",
    }
    values.update(overrides)
    return service.propose(company_id, policy_id=policy_id, content=content, **values)


def _review(service, company_id, policy_id, memory_id, decision="ACCEPT"):
    return service.review(
        company_id,
        memory_id,
        policy_id=policy_id,
        decision=decision,
        reviewer="owner",
        reviewer_roles={"TENANT_ADMIN"},
        reason="Evidence checked for an isolated regression test.",
    )


def _search(service, company_id, policy_id, **overrides):
    values = {
        "namespaces": [(MemoryNamespaceType.COMPANY, str(company_id))],
        "memory_types": [MemoryType.FACT],
        "query": "report",
        "reason": "Assess only eligible canonical records.",
        "principal_id": "owner",
    }
    values.update(overrides)
    return service.search(company_id, policy_id=policy_id, **values)


def _assert_no_competing_records(match):
    assert match.conflict_status is MemoryConflictStatus.NO_COMPETING_RECORDS
    assert match.conflict is False
    assert match.conflict_reason == "no_competing_active_contents"
    assert match.competing_memory_ids == ()
    assert match.competing_memory_count == 0
    assert match.competing_ids_truncated is False


@pytest.mark.parametrize(
    "sensitivity", [MemorySensitivity.CONFIDENTIAL, MemorySensitivity.RESTRICTED]
)
def test_forbidden_sensitivity_cannot_influence_competing_ids(
    company_service, organizational_memory_service, sensitivity
):
    service = organizational_memory_service
    company = _company(company_service)
    writer = _policy(service, company.id)
    reader = _policy(service, company.id, key="reader", forbidden=[sensitivity])
    visible = _note(service, company.id, writer.id, "Report cadence is weekly.")
    hidden = _note(
        service,
        company.id,
        writer.id,
        "Report cadence is quarterly.",
        sensitivity=sensitivity,
    )
    for note in (visible, hidden):
        _review(service, company.id, writer.id, note.memory.id)

    unrestricted = _search(service, company.id, writer.id)
    assert all(
        match.conflict_status is MemoryConflictStatus.REVIEW_REQUIRED
        for match in unrestricted.matches
    )
    restricted = _search(service, company.id, reader.id)
    assert [match.memory.id for match in restricted.matches] == [visible.memory.id]
    _assert_no_competing_records(restricted.matches[0])
    assert restricted.retrieval.result_memory_ids == [visible.memory.id]


@pytest.mark.parametrize(
    "excluded_status",
    [MemoryStatus.CANDIDATE, MemoryStatus.REJECTED, MemoryStatus.REVOKED, MemoryStatus.EXPIRED],
)
def test_ineligible_lifecycle_record_cannot_influence_competing_ids(
    company_service, organizational_memory_service, excluded_status
):
    service = organizational_memory_service
    company = _company(company_service)
    policy = _policy(service, company.id)
    visible = _note(service, company.id, policy.id, "Report cadence is weekly.")
    _review(service, company.id, policy.id, visible.memory.id)
    expires_at = utc_now() + timedelta(minutes=1)
    hidden = _note(
        service,
        company.id,
        policy.id,
        "Report cadence is quarterly.",
        expires_at=expires_at if excluded_status is MemoryStatus.EXPIRED else None,
    )
    if excluded_status is MemoryStatus.REJECTED:
        _review(service, company.id, policy.id, hidden.memory.id, decision="REJECT")
    elif excluded_status in {MemoryStatus.REVOKED, MemoryStatus.EXPIRED}:
        _review(service, company.id, policy.id, hidden.memory.id)
        if excluded_status is MemoryStatus.REVOKED:
            service.revoke(company.id, hidden.memory.id, reviewer="owner", reason="Withdrawn.")

    result = _search(service, company.id, policy.id, now=expires_at)
    assert [match.memory.id for match in result.matches] == [visible.memory.id]
    _assert_no_competing_records(result.matches[0])
    assert service.get_memory(company.id, hidden.memory.id).memory.status is excluded_status


def test_disallowed_and_unrequested_types_cannot_influence_competing_ids(
    company_service, organizational_memory_service
):
    service = organizational_memory_service
    company = _company(company_service)
    writer = _policy(service, company.id)
    reader = _policy(service, company.id, key="facts-only", allowed=[MemoryType.FACT])
    fact = _note(service, company.id, writer.id, "Report cadence is weekly.")
    decision = _note(
        service,
        company.id,
        writer.id,
        "Report cadence is quarterly.",
        memory_type=MemoryType.DECISION,
    )
    for note in (fact, decision):
        _review(service, company.id, writer.id, note.memory.id)

    for policy in (writer, reader):
        result = _search(service, company.id, policy.id)
        assert [match.memory.id for match in result.matches] == [fact.memory.id]
        _assert_no_competing_records(result.matches[0])
    both = _search(
        service, company.id, writer.id, memory_types=[MemoryType.FACT, MemoryType.DECISION]
    )
    assert {match.memory.id for match in both.matches} == {fact.memory.id, decision.memory.id}
    for match in both.matches:
        _assert_no_competing_records(match)
    with pytest.raises(OrganizationalMemoryConflict, match="disallowed Memory Type"):
        _search(
            service, company.id, reader.id, memory_types=[MemoryType.FACT, MemoryType.DECISION]
        )


def test_same_subject_in_other_company_cannot_influence_competing_ids(
    company_service, organizational_memory_service
):
    service = organizational_memory_service
    records = []
    for content in ["Report cadence is weekly.", "Report cadence is quarterly."]:
        company = _company(company_service)
        policy = _policy(service, company.id)
        note = _note(
            service,
            company.id,
            policy.id,
            content,
            namespace_type=MemoryNamespaceType.EMPLOYEE,
            namespace_id="researcher",
        )
        _review(service, company.id, policy.id, note.memory.id)
        records.append((company, policy, note))
        company_service.archive_company(company.id)
    for company, policy, note in records:
        result = _search(
            service,
            company.id,
            policy.id,
            namespaces=[(MemoryNamespaceType.EMPLOYEE, "researcher")],
        )
        assert [match.memory.id for match in result.matches] == [note.memory.id]
        _assert_no_competing_records(result.matches[0])


@pytest.mark.parametrize(
    "other_namespace",
    [(MemoryNamespaceType.USER, "researcher"), (MemoryNamespaceType.EMPLOYEE, "analyst")],
)
def test_namespace_type_and_id_each_isolate_subject_assessment(
    company_service, organizational_memory_service, other_namespace
):
    service = organizational_memory_service
    company = _company(company_service)
    policy = _policy(service, company.id)
    namespaces = [(MemoryNamespaceType.EMPLOYEE, "researcher"), other_namespace]
    notes = []
    for namespace, content in zip(
        namespaces, ["Report cadence is weekly.", "Report cadence is quarterly."], strict=True
    ):
        note = _note(
            service,
            company.id,
            policy.id,
            content,
            namespace_type=namespace[0],
            namespace_id=namespace[1],
        )
        _review(service, company.id, policy.id, note.memory.id)
        notes.append(note)
    result = _search(service, company.id, policy.id, namespaces=namespaces)
    assert {match.memory.id for match in result.matches} == {note.memory.id for note in notes}
    for match in result.matches:
        _assert_no_competing_records(match)


def test_duplicate_digest_is_rejected_without_creating_review_required_result(
    company_service, organizational_memory_service
):
    service = organizational_memory_service
    company = _company(company_service)
    policy = _policy(service, company.id)
    first = _note(service, company.id, policy.id, "Report cadence is weekly.")
    _review(service, company.id, policy.id, first.memory.id)
    with pytest.raises(OrganizationalMemoryConflict, match="Duplicate Memory"):
        _note(service, company.id, policy.id, "  Report cadence is weekly.  ")
    result = _search(service, company.id, policy.id)
    assert [match.memory.id for match in result.matches] == [first.memory.id]
    _assert_no_competing_records(result.matches[0])


def test_identical_digests_do_not_count_as_competing_content(
    company_service, organizational_memory_service
):
    service = organizational_memory_service
    company = _company(company_service)
    policy = _policy(service, company.id)
    first = _note(service, company.id, policy.id, "Report cadence is weekly.")
    accepted = _review(service, company.id, policy.id, first.memory.id).memory
    # Duplicate persisted contents are blocked by propose(); exercise the defensive
    # assessment branch using a domain record without bypassing repository constraints.
    same_content = replace(accepted, id=uuid4())
    assessments = OrganizationalMemoryService._conflict_assessments([accepted, same_content])
    for assessment in assessments.values():
        assert assessment[0] is MemoryConflictStatus.NO_COMPETING_RECORDS
        assert assessment[1] == "no_competing_active_contents"
        assert assessment[2] == ()


def _many_competing_records(service, company_service):
    company = _company(company_service)
    policy = _policy(service, company.id)
    memory_ids = []
    for index in range(13):
        note = _note(
            service,
            company.id,
            policy.id,
            f"Report cadence variant {index:02d}.",
            subject_key="r" + "a" * 127,
        )
        _review(service, company.id, policy.id, note.memory.id)
        memory_ids.append(note.memory.id)
    return company, policy, memory_ids


def test_competing_id_metadata_is_bounded_with_exact_count_and_deterministic_sample(
    company_service, organizational_memory_service
):
    service = organizational_memory_service
    company, policy, memory_ids = _many_competing_records(service, company_service)
    first = _search(service, company.id, policy.id, maximum_count=1)
    repeated = _search(service, company.id, policy.id, maximum_count=1)
    assert len(first.matches) == len(repeated.matches) == 1
    match = first.matches[0]
    expected = tuple(sorted((value for value in memory_ids if value != match.memory.id), key=str))
    assert match.conflict_status is MemoryConflictStatus.REVIEW_REQUIRED
    assert match.conflict is None
    assert match.competing_memory_count == 12
    assert match.competing_ids_truncated is True
    assert match.competing_memory_ids == expected[:10]
    assert repeated.matches[0].competing_memory_ids == match.competing_memory_ids
    context = memory_context_record(match)
    assert context["competing_memory_count"] == 12
    assert context["competing_ids_truncated"] is True
    assert context["competing_memory_ids"] == [str(value) for value in expected[:10]]


def test_context_budget_includes_long_subject_and_competing_id_metadata(
    company_service, organizational_memory_service
):
    service = organizational_memory_service
    company, policy, _ = _many_competing_records(service, company_service)
    baseline = _search(service, company.id, policy.id, maximum_count=1)
    assert len(baseline.matches) == 1
    match = baseline.matches[0]
    serialized_length = len(json.dumps(memory_context_record(match), ensure_ascii=False))
    serialized_tokens = (serialized_length + 2 + 3) // 4
    content_only_tokens = max(1, len(match.memory.content) // 4)
    assert serialized_tokens > content_only_tokens

    too_small = _search(
        service,
        company.id,
        policy.id,
        maximum_count=1,
        maximum_context_tokens=serialized_tokens - 1,
    )
    assert too_small.matches == []
    assert too_small.retrieval.result_memory_ids == []
    exact_budget = _search(
        service,
        company.id,
        policy.id,
        maximum_count=1,
        maximum_context_tokens=serialized_tokens,
    )
    assert [value.memory.id for value in exact_budget.matches] == [match.memory.id]
    context_tokens = sum(
        (len(json.dumps(memory_context_record(value), ensure_ascii=False)) + 5) // 4
        for value in exact_budget.matches
    )
    assert context_tokens <= serialized_tokens
