import json
from uuid import uuid4

import httpx

from agentmesh.domain.organizational_memory import (
    MemoryNamespaceType,
    MemoryProvenanceType,
    MemoryRecord,
    MemorySensitivity,
    MemoryType,
)
from agentmesh.integrations.memos_memory import MemOSCloudClient, MemOSMemoryRankingBackend


def _memory(company_id, content):
    value = MemoryRecord.propose(
        company_id=company_id,
        namespace_type=MemoryNamespaceType.COMPANY,
        namespace_id=str(company_id),
        memory_type=MemoryType.FACT,
        content=content,
        provenance_type=MemoryProvenanceType.USER_STATEMENT,
        provenance_id=str(uuid4()),
        confidence_basis_points=8_000,
        sensitivity=MemorySensitivity.INTERNAL,
    )
    value.accept("owner")
    return value


def test_memos_sync_and_ranking_preserve_canonical_candidate_set():
    company_id = uuid4()
    first = _memory(company_id, "The customer prefers concise release notes.")
    second = _memory(company_id, "The company uses a reviewed launch checklist.")
    calls = []

    def handler(request):
        assert request.url.host == "memos.memtensor.cn"
        assert request.headers["Authorization"] == "Token test-only-key"
        assert request.url.path.startswith("/api/openmem/v1/")
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        if request.url.path.endswith("/delete/memory"):
            return httpx.Response(200, json={"code": 0, "data": {"success": True}})
        if request.url.path.endswith("/add/message"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"success": True, "status": "completed"}},
            )
        assert request.url.path.endswith("/search/memory")
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "memory_detail_list": [
                        {"conversation_id": f"agentmesh-{second.id}"},
                        {"conversation_id": f"agentmesh-{uuid4()}"},
                    ],
                    "preference_detail_list": [],
                },
            },
        )

    backend = MemOSMemoryRankingBackend(
        client=MemOSCloudClient(
            api_key="test-only-key", transport=httpx.MockTransport(handler)
        ),
        tenant_id="test-tenant",
        allowed_company_id=company_id,
    )
    assert backend.sync([first, second]) == 2
    assert [item.id for item in backend.rank("launch", [first, second])] == [
        second.id,
        first.id,
    ]
    assert len(calls) == 5
    assert calls[2][1]["allow_public"] is False
    assert calls[2][1]["async_mode"] is False


def test_memos_failure_and_unapproved_company_fall_back_without_remote_egress():
    company_id = uuid4()
    other = _memory(uuid4(), "This record belongs to another company.")
    memory = _memory(company_id, "The reviewed record stays available locally.")
    calls = []

    def failing(request):
        calls.append(request.url.path)
        return httpx.Response(503, text="unavailable")

    backend = MemOSMemoryRankingBackend(
        client=MemOSCloudClient(
            api_key="test-only-key", transport=httpx.MockTransport(failing)
        ),
        tenant_id="test-tenant",
        allowed_company_id=company_id,
    )
    assert backend.rank("record", [other]) == [other]
    assert calls == []
    assert backend.rank("record", [memory]) == [memory]
    assert len(calls) == 1
    memory.sensitivity = MemorySensitivity.CONFIDENTIAL
    assert backend.rank("record", [memory]) == [memory]
    assert len(calls) == 1
